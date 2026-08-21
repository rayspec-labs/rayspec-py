# SPDX-License-Identifier: Apache-2.0
"""The local spend ledger — cross-run budget state for ONE user on ONE machine.

Module boundary: a single JSON file plus the locking that makes concurrent writers safe. It
knows nothing about workflows, providers or policy; :mod:`rayspec.limits.envelope` decides what
the numbers mean.

Shape and scope, deliberately:

* it lives beside the project's runs (``<store root>/limits/spend.json``), so it is per user
  (``$RAYSPEC_HOME``) and per project, exactly like the run store itself;
* it holds day and month totals, a per-run committed amount and a consecutive-failure counter.
  No user, no team, no repository, no tag — there is nothing here to roll up;
* every writer takes an exclusive ``flock`` on the file for the whole read-modify-write, so two
  runs finishing in the same instant both land. Without ``fcntl`` (Windows) the write still
  happens, single-writer;
* a run commits its ABSOLUTE total, not a delta, so committing twice (a resume, a retry, a
  crash between commit and record write) can never double-count.

Nothing in this file is a secret: run ids, dates, counts and dollar amounts.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from rayspec.store.file import PRIVATE_FILE_MODE, secure_mkdir

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

#: Format version of ``spend.json``.
LEDGER_VERSION = 1

#: How long a per-run entry and its day bucket are kept. Two months is enough for a monthly
#: envelope to be exact and short enough that the file cannot grow without bound.
RETAIN_DAYS = 62

#: How many month buckets are kept (a monthly envelope only ever reads the current one).
RETAIN_MONTHS = 24


def ledger_path(store_root: Path) -> Path:
    """``<store root>/limits/spend.json`` — beside ``runs/``, never inside a run directory."""
    return Path(store_root) / "limits" / "spend.json"


@dataclass(frozen=True, slots=True)
class SpendState:
    """What the ledger says right now: this day, this month and the failure streak."""

    day_usd: float = 0.0
    month_usd: float = 0.0
    consecutive_failures: int = 0


def _day_key(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d")


def _month_key(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m")


class SpendLedger:
    """Read/modify/write ``spend.json`` under an exclusive lock.

    Every method is synchronous and does blocking IO — callers on the event loop run it through
    ``anyio.to_thread``.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- public surface -------------------------------------------------------------------

    def read(self, *, when: datetime | None = None) -> SpendState:
        """The totals for the day/month of ``when`` (default: now) and the failure streak."""
        moment = when or datetime.now(UTC)
        with self._locked() as (_fd, data):
            return _state_of(data, moment)

    def commit(self, run_id: str, cost_usd: float | None, *, when: datetime) -> SpendState:
        """Record that ``run_id`` has spent ``cost_usd`` IN TOTAL, and return the new state.

        ``when`` is the run's start: a run that crosses midnight keeps accruing into the day it
        began, so one run is never split across two envelopes. A run with an unknown cost
        (``None``) commits nothing but still gets an entry, so its day is fixed from the start.
        """
        amount = float(cost_usd) if cost_usd is not None else 0.0
        with self._locked() as (fd, data):
            runs = data.setdefault("runs", {})
            entry = runs.get(run_id)
            if entry is None:
                entry = {"cost_usd": 0.0, "day": _day_key(when), "month": _month_key(when)}
                runs[run_id] = entry
            delta = amount - float(entry.get("cost_usd") or 0.0)
            entry["cost_usd"] = amount
            if delta:
                _add(data.setdefault("days", {}), str(entry["day"]), delta)
                _add(data.setdefault("months", {}), str(entry["month"]), delta)
            _prune(data, when)
            self._write(fd, data)
            return _state_of(data, when)

    def record_outcome(self, *, failed: bool) -> int:
        """Advance the consecutive-failure counter (or reset it) and return its new value."""
        with self._locked() as (fd, data):
            value = int(data.get("consecutive_failures") or 0) + 1 if failed else 0
            data["consecutive_failures"] = value
            self._write(fd, data)
            return value

    def reset_failures(self) -> None:
        """Close the circuit breaker again (an operator decided the run may continue)."""
        with self._locked() as (fd, data):
            data["consecutive_failures"] = 0
            self._write(fd, data)

    # -- internals ------------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Any:
        """Open the file, hold an exclusive lock and yield ``(fd, data)``.

        The lock covers the whole read-modify-write; the file descriptor stays open so the
        rewrite happens under the same lock (a rename would drop it).
        """
        secure_mkdir(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd, self._read(fd)
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _read(fd: int) -> dict[str, Any]:
        """The parsed document, or a fresh one when the file is empty or unreadable.

        A ledger that cannot be parsed is REPLACED, never raised: losing the accrued total is a
        smaller failure than refusing to run at all, and the next commit rebuilds it.
        """
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw.strip():
            return {"version": LEDGER_VERSION}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"version": LEDGER_VERSION}
        if not isinstance(data, dict):
            return {"version": LEDGER_VERSION}
        return data

    @staticmethod
    def _write(fd: int, data: dict[str, Any]) -> None:
        data["version"] = LEDGER_VERSION
        data["updated_at"] = datetime.now(UTC).isoformat()
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)


def _add(bucket: dict[str, Any], key: str, delta: float) -> None:
    bucket[key] = round(float(bucket.get(key) or 0.0) + delta, 10)


def _state_of(data: dict[str, Any], when: datetime) -> SpendState:
    days = data.get("days") or {}
    months = data.get("months") or {}
    return SpendState(
        day_usd=float(days.get(_day_key(when)) or 0.0) if isinstance(days, dict) else 0.0,
        month_usd=float(months.get(_month_key(when)) or 0.0) if isinstance(months, dict) else 0.0,
        consecutive_failures=int(data.get("consecutive_failures") or 0),
    )


def _prune(data: dict[str, Any], now: datetime) -> None:
    """Drop day buckets and run entries older than :data:`RETAIN_DAYS`, months beyond
    :data:`RETAIN_MONTHS`."""
    cutoff_day = _day_key(now - timedelta(days=RETAIN_DAYS))
    days = data.get("days")
    if isinstance(days, dict):
        data["days"] = {k: v for k, v in days.items() if k >= cutoff_day}
    months = data.get("months")
    if isinstance(months, dict) and len(months) > RETAIN_MONTHS:
        keep = sorted(months)[-RETAIN_MONTHS:]
        data["months"] = {k: months[k] for k in keep}
    runs = data.get("runs")
    if isinstance(runs, dict):
        data["runs"] = {
            k: v
            for k, v in runs.items()
            if isinstance(v, dict) and str(v.get("day") or "") >= cutoff_day
        }


__all__ = [
    "LEDGER_VERSION",
    "RETAIN_DAYS",
    "RETAIN_MONTHS",
    "SpendLedger",
    "SpendState",
    "ledger_path",
]
