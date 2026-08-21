# SPDX-License-Identifier: Apache-2.0
"""Host-level run slots — how many runs of one provider this machine starts at once.

Module boundary: ``flock`` files under ``$RAYSPEC_HOME/limits/slots/<provider>/<n>.lock`` and
nothing else. It has no opinion about what a run is; the CLI holds the slots around one run.

Modelled on :mod:`rayspec.workspace.lock` (the per-workdir ``PathLock``), and for the same
reason: an advisory ``flock`` is released by the kernel when the holder dies, so a slot held by
a process that was killed, crashed or lost power is free again the instant that process is gone.
Nothing has to detect a stale holder, and no lock file is ever unlinked (the classic unlink race
would let two runs into one slot). The holder JSON — run id, pid, start time — is a courtesy for
the "who has the slots?" message, never the source of truth.

Local by construction: the slots are files in one user's home on one machine. There is no
registry, no server and no cross-host coordination here.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from rayspec.errors import RayspecError
from rayspec.store.file import PRIVATE_FILE_MODE, secure_mkdir

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

#: How often a waiting run re-tries the slot files.
SLOT_POLL_S = 0.25

#: ``--wait-slot`` given as a bare flag: wait for as long as it takes.
WAIT_FOREVER = "forever"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class SlotBusyError(RayspecError):
    """Every run slot of a provider is taken (and the caller did not want to wait)."""

    def __init__(self, message: str, *, provider: str, limit: int, hint: str | None = None):
        super().__init__(message, hint=hint)
        self.provider = provider
        self.limit = limit


@dataclass(frozen=True, slots=True)
class SlotHolder:
    """Who a slot file says holds it (may be stale — the ``flock`` is the truth)."""

    provider: str
    index: int
    run_id: str | None = None
    pid: int | None = None
    started_at: str | None = None

    def describe(self) -> str:
        """``run 20260821-… (pid 4711)`` for the "host is busy" message."""
        run = self.run_id or "unknown"
        return f"run {run} (pid {self.pid})" if self.pid else f"run {run}"


def slot_dir(home: Path, provider: str) -> Path:
    """``<home>/limits/slots/<provider>/`` (not created)."""
    return Path(home) / "limits" / "slots" / _UNSAFE.sub("_", provider or "unknown")


def slot_path(home: Path, provider: str, index: int) -> Path:
    """The lock file of slot ``index`` (1-based)."""
    return slot_dir(home, provider) / f"{index}.lock"


def read_holder(path: Path, provider: str, index: int) -> SlotHolder:
    """Parse a slot file's holder JSON (an unreadable/empty file yields an empty holder)."""
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SlotHolder(provider=provider, index=index)
    if not isinstance(raw, dict):
        return SlotHolder(provider=provider, index=index)
    pid = raw.get("pid")
    return SlotHolder(
        provider=provider,
        index=index,
        run_id=str(raw["run_id"]) if raw.get("run_id") is not None else None,
        pid=int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None,
        started_at=str(raw["started_at"]) if raw.get("started_at") is not None else None,
    )


class RunSlot:
    """One of ``limit`` run slots for a provider; a context manager, like ``PathLock``.

    ``acquire()`` walks the slot files in order and takes the first free one. ``wait_s=None``
    does not wait at all (raise :class:`SlotBusyError`), ``math.inf`` waits indefinitely,
    anything else is a deadline in seconds.

    A ``limit`` of ``0`` is a real limit — this provider may not run on this host — and every
    ``acquire()`` raises :class:`SlotBusyError` immediately, however long the caller offered to
    wait. Waiting for a slot that cannot exist is never the answer.
    """

    def __init__(self, home: Path, provider: str, limit: int, *, run_id: str) -> None:
        if limit < 0:
            raise ValueError("a run-slot limit must not be negative")
        self.home = Path(home)
        self.provider = provider
        self.limit = limit
        self.run_id = run_id
        self.index: int | None = None
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """True while this object holds a slot."""
        return self._fd is not None

    @property
    def path(self) -> Path | None:
        """The lock file currently held (``None`` when free)."""
        return slot_path(self.home, self.provider, self.index) if self.index else None

    def acquire(self, *, wait_s: float | None = None, poll_s: float = SLOT_POLL_S) -> RunSlot:
        """Take a free slot, waiting per ``wait_s``; raise :class:`SlotBusyError` otherwise."""
        if self._fd is not None:
            return self
        if self.limit == 0:
            raise self._disabled_error()
        if fcntl is None:  # pragma: no cover - Windows only
            self.index = 0  # no flock: the limit cannot be enforced, and must not block a run
            return self
        forever = wait_s is not None and math.isinf(wait_s)
        deadline = None if wait_s is None or forever else time.monotonic() + float(wait_s)
        while True:
            if self._try_all():
                return self
            if wait_s is None:
                raise self._busy_error(waited=False)
            if deadline is not None and time.monotonic() >= deadline:
                raise self._busy_error(waited=True, wait_s=float(wait_s))
            time.sleep(poll_s)

    def release(self) -> None:
        """Give the slot back (no-op when not held); the file stays but is emptied."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        self.index = None
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def holders(self) -> list[SlotHolder]:
        """What the slot files say about their holders (best effort, possibly stale)."""
        return [
            read_holder(slot_path(self.home, self.provider, i), self.provider, i)
            for i in range(1, self.limit + 1)
        ]

    # -- internals ------------------------------------------------------------------------

    def _try_all(self) -> bool:
        return any(self._try_one(index) for index in range(1, self.limit + 1))

    def _try_one(self, index: int) -> bool:
        path = slot_path(self.home, self.provider, index)
        secure_mkdir(path.parent)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
        try:
            assert fcntl is not None
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                return False
            _write_holder(fd, self.provider, index, self.run_id)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        self.index = index
        return True

    def _disabled_error(self) -> SlotBusyError:
        """``limit == 0``: the operator switched this provider off on this host."""
        return SlotBusyError(
            f"{self.provider} runs are switched off on this host "
            f"(policy max_concurrent_runs {self.provider} is 0)",
            provider=self.provider,
            limit=0,
            hint="raise policy max_concurrent_runs to allow a run",
        )

    def _busy_error(self, *, waited: bool, wait_s: float | None = None) -> SlotBusyError:
        who = "; ".join(h.describe() for h in self.holders() if h.run_id) or "other runs"
        plural = "" if self.limit == 1 else "s"
        prefix = (
            f"no free {self.provider} run slot after waiting {wait_s:g}s"
            if waited
            else f"all {self.limit} {self.provider} run slot{plural} on this host are taken"
        )
        hint = (
            "wait with --wait-slot, or raise policy max_concurrent_runs"
            if not waited
            else "raise policy max_concurrent_runs, or wait longer"
        )
        return SlotBusyError(
            f"{prefix} ({who})", provider=self.provider, limit=self.limit, hint=hint
        )

    def __enter__(self) -> RunSlot:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        state = f"slot {self.index}" if self.held else "free"
        return f"RunSlot({self.provider}, limit={self.limit}, {state})"


def _write_holder(fd: int, provider: str, index: int, run_id: str) -> None:
    payload = json.dumps(
        {
            "provider": provider,
            "slot": index,
            "run_id": run_id,
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
        },
        indent=2,
    ).encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(payload)
    while view:
        view = view[os.write(fd, view) :]
    os.fsync(fd)


@contextlib.contextmanager
def acquire_slots(
    home: Path,
    providers: Iterable[str],
    limits: dict[str, int],
    *,
    run_id: str,
    wait_s: float | None = None,
) -> Iterator[Sequence[RunSlot]]:
    """Hold one slot per capped provider for the duration of the block.

    Providers are taken in sorted order so two runs asking for the same pair never deadlock, and
    a provider without a limit is skipped entirely (a limit of ``0`` is not "no limit": it
    refuses the run). Everything acquired is released on the way out, including when acquiring a
    later slot fails.
    """
    held: list[RunSlot] = []
    try:
        for provider in sorted({p for p in providers if limits.get(p) is not None}):
            slot = RunSlot(home, provider, int(limits[provider]), run_id=run_id)
            slot.acquire(wait_s=wait_s)
            held.append(slot)
        yield held
    finally:
        for slot in reversed(held):
            slot.release()


__all__ = [
    "SLOT_POLL_S",
    "WAIT_FOREVER",
    "RunSlot",
    "SlotBusyError",
    "SlotHolder",
    "acquire_slots",
    "read_holder",
    "slot_dir",
    "slot_path",
]
