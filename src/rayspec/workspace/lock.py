# SPDX-License-Identifier: Apache-2.0
"""Per-workdir path lock (``fcntl.flock``) so two runs never share a working directory.

Boundary: one lock file per (project slug, workdir) under ``<home>/projects/<slug>/locks/``.
POSIX-first: on platforms without ``fcntl`` (Windows) :meth:`PathLock.acquire` raises
``NotImplementedError`` with a clear message. The lock is auto-released by the kernel when the
holding process dies; the file itself is never unlinked (avoids the classic unlink race).
Filesystem failures (e.g. ``locks`` occupied by a file) surface as :class:`WorkspaceError`.
The lock is usually the first writer under ``$RAYSPEC_HOME`` on a run, so it creates the missing
directories ``0700`` and the lock file ``0600`` (:mod:`rayspec.store.file` helpers).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from rayspec.store.file import PRIVATE_FILE_MODE, secure_mkdir
from rayspec.workspace.errors import WorkdirLockedError, WorkspaceError
from rayspec.workspace.project import project_dir

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class LockHolder:
    """Content of a held lock file: ``{run_id, pid, workdir, started_at}``."""

    run_id: str | None
    pid: int | None
    workdir: str | None
    started_at: str | None


def lock_path(home: Path, slug: str, workdir: Path) -> Path:
    """``<home>/projects/<slug>/locks/<sha1(resolved workdir)>.lock``."""
    digest = hashlib.sha1(str(Path(workdir).resolve()).encode("utf-8")).hexdigest()
    return project_dir(home, slug) / "locks" / f"{digest}.lock"


def _write_all(fd: int, data: bytes) -> None:
    """``os.write`` until every byte is out (short writes are legal for regular files too)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def read_lock_holder(path: Path) -> LockHolder | None:
    """Parse the holder JSON of ``path`` (``None`` when absent, empty or unparseable)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data: Any = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    return LockHolder(
        run_id=str(data["run_id"]) if data.get("run_id") is not None else None,
        pid=int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None,
        workdir=str(data["workdir"]) if data.get("workdir") is not None else None,
        started_at=str(data["started_at"]) if data.get("started_at") is not None else None,
    )


def remove_lock_file(home: Path, slug: str, workdir: Path) -> bool:
    """Unlink the lock file of ``workdir`` when nobody holds it (``worktrees clean``).

    Takes the lock non-blocking first, so a file that a live run holds is left alone (the kernel
    keeps the lock; unlinking it would let a second run slip in). Returns ``True`` when a file was
    removed; a missing file, a held lock or an OS error ⇒ ``False``.
    """
    path = lock_path(home, slug, workdir)
    if not path.exists():
        return False
    lock = PathLock(home, slug, workdir, run_id="")
    try:
        lock.acquire()
    except (WorkspaceError, NotImplementedError, OSError):
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    finally:
        lock.release()
    return True


class PathLock:
    """Exclusive ``flock`` on the lock file for ``workdir``; usable as a context manager.

    ``acquire()`` is non-blocking by default and raises :class:`WorkdirLockedError` naming the
    current holder (``already locked by run X (pid Y)``). Re-acquiring a held lock is a no-op;
    ``release()`` is idempotent. The file keeps its JSON content while held and is truncated on
    release so readers can tell a free lock from a stale one.
    """

    def __init__(self, home: Path, slug: str, workdir: Path, *, run_id: str):
        self.home = home
        self.slug = slug
        self.workdir = Path(workdir).resolve()
        self.run_id = run_id
        self.path = lock_path(home, slug, workdir)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """True while this object holds the lock."""
        return self._fd is not None

    def acquire(self, *, blocking: bool = False) -> PathLock:
        """Take the lock (``blocking=True`` waits for the holder to release it)."""
        if fcntl is None:  # pragma: no cover - Windows only
            raise NotImplementedError(
                "rayspec path locks need fcntl.flock, which is unavailable on this platform "
                "(Windows); run with isolation 'none' and a single run per directory"
            )
        if self._fd is not None:
            return self
        try:
            secure_mkdir(self.path.parent)  # creates $RAYSPEC_HOME/projects/<slug> 0700
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
        except OSError as exc:
            raise WorkspaceError(
                f"cannot create lock file {self.path}: {exc}",
                hint="check permissions under the rayspec home and that 'locks' is a directory",
            ) from exc
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError:
                holder = read_lock_holder(self.path)
                run = holder.run_id if holder and holder.run_id else "unknown"
                pid = holder.pid if holder else None
                raise WorkdirLockedError(
                    f"{self.workdir} is already locked by run {run} (pid {pid})",
                    workdir=str(self.workdir),
                    run_id=holder.run_id if holder else None,
                    pid=pid,
                    hint="wait for that run to finish, or run it in another worktree",
                ) from None
            content = json.dumps(
                {
                    "run_id": self.run_id,
                    "pid": os.getpid(),
                    "workdir": str(self.workdir),
                    "started_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, content.encode("utf-8"))
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        """Drop the lock (no-op when not held); the file stays but is emptied."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def holder(self) -> LockHolder | None:
        """The current holder as recorded in the lock file (may be stale)."""
        return read_lock_holder(self.path)

    def __enter__(self) -> PathLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __repr__(self) -> str:
        state = "held" if self.held else "free"
        return f"PathLock({self.path}, run_id={self.run_id!r}, {state})"


__all__ = ["LockHolder", "PathLock", "lock_path", "read_lock_holder", "remove_lock_file"]
