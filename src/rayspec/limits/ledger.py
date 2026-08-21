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
* every writer takes an exclusive ``flock`` on a sibling lock file for the whole
  read-modify-write, so two runs finishing in the same instant both land. The document itself is
  then replaced whole (temp file + ``os.replace``), so a crash mid-write cannot leave a
  half-written ledger — which would silently reset the day, the month and the failure streak.
  Without ``fcntl`` (Windows) the write still happens, single-writer;
* a run commits its ABSOLUTE total, not a delta, so committing twice (a resume, a retry, a
  crash between commit and record write) can never double-count. The DELTA between two commits
  is attributed to the day and month the commit is made in: a run resumed on Thursday spends
  Thursday's money, or every other run started on Thursday would get headroom nobody granted;
* a per-run entry is kept for :data:`RETAIN_DAYS` after its LAST commit, so a run that is still
  being resumed is never re-baselined. A run resumed after longer than that has been forgotten
  and its next commit counts as fresh spend.

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
    ``anyio.to_thread``. Anything the ledger had to paper over (a document it could not parse
    and replaced) is collected in :meth:`take_warnings` for the caller to report; a control that
    resets itself in silence is indistinguishable from one that was never there.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.warnings: list[str] = []

    # -- public surface -------------------------------------------------------------------

    def take_warnings(self) -> list[str]:
        """Drain what the ledger has to say about itself (empty when all was well)."""
        warnings, self.warnings = self.warnings, []
        return warnings

    def read(self, *, when: datetime | None = None) -> SpendState:
        """The totals for the day/month of ``when`` (default: now) and the failure streak."""
        moment = when or datetime.now(UTC)
        with self._locked() as (_fd, data):
            return _state_of(data, moment)

    def commit(
        self, run_id: str, cost_usd: float | None, *, when: datetime | None = None
    ) -> SpendState:
        """Record that ``run_id`` has spent ``cost_usd`` IN TOTAL, and return the new state.

        ``when`` is the moment of the commit (default: now). Only the DIFFERENCE against what
        this run had already committed is added, and it is added to that moment's day and month:
        a run that crosses midnight pays the second half into the second day, which is where the
        money was actually spent. A run with an unknown cost (``None``) commits nothing but
        still gets an entry.
        """
        moment = when or datetime.now(UTC)
        amount = float(cost_usd) if cost_usd is not None else 0.0
        with self._locked() as (fd, data):
            runs = data.setdefault("runs", {})
            entry = runs.get(run_id)
            if not isinstance(entry, dict):
                entry = {}
                runs[run_id] = entry
            delta = amount - float(entry.get("cost_usd") or 0.0)
            # the LAST commit's buckets: what the pruning clock is measured from
            entry.update(
                {
                    "cost_usd": amount,
                    "day": _day_key(moment),
                    "month": _month_key(moment),
                }
            )
            if delta:
                _add(data.setdefault("days", {}), _day_key(moment), delta)
                _add(data.setdefault("months", {}), _month_key(moment), delta)
            _prune(data, moment)
            self._write(fd, data)
            return _state_of(data, moment)

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

    @property
    def lock_path(self) -> Path:
        """The sibling file the ``flock`` is held on.

        A separate file because the ledger itself is REPLACED on every write: a lock on an inode
        that is about to be unlinked would let the next writer in through the new one.
        """
        return self.path.with_name(self.path.name + ".lock")

    @contextlib.contextmanager
    def _locked(self) -> Any:
        """Hold the exclusive lock for a whole read-modify-write and yield ``(fd, data)``."""
        secure_mkdir(self.path.parent)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd, self._read()
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict[str, Any]:
        """The parsed document, or a fresh one when the file is missing or unreadable.

        A ledger that cannot be parsed is REPLACED, never raised: losing the accrued total is a
        smaller failure than refusing to run at all, and the next commit rebuilds it. It is also
        recorded in :attr:`warnings`, because an envelope that quietly went back to zero is
        exactly what an operator must not have to discover for themselves.
        """
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {"version": LEDGER_VERSION}
        except OSError as exc:
            return self._fresh(f"cannot be read ({exc.strerror or exc})")
        if not raw.strip():
            return {"version": LEDGER_VERSION}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._fresh("is not readable JSON")
        if not isinstance(data, dict):
            return self._fresh("is not a ledger document")
        return data

    def _fresh(self, why: str) -> dict[str, Any]:
        self.warnings.append(
            f"the spend ledger {self.path} {why} — the accrued day, month and failure "
            "totals start again from zero"
        )
        return {"version": LEDGER_VERSION}

    def _write(self, _fd: int, data: dict[str, Any]) -> None:
        """Replace the document whole, under the lock the caller is holding."""
        data["version"] = LEDGER_VERSION
        data["updated_at"] = datetime.now(UTC).isoformat()
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
            try:
                view = memoryview(payload)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise


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
    :data:`RETAIN_MONTHS`. A run entry's age is that of its LAST commit."""
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
