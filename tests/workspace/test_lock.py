"""PathLock: flock exclusivity within and across processes."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from rayspec.workspace.errors import WorkdirLockedError
from rayspec.workspace.lock import PathLock, lock_path, read_lock_holder


def test_lock_path(home: Path) -> None:
    workdir = home / "wd"
    digest = hashlib.sha1(str(workdir.resolve()).encode()).hexdigest()
    assert lock_path(home, "github.com/o/r", workdir) == (
        home / "projects" / "github.com" / "o" / "r" / "locks" / f"{digest}.lock"
    )


def test_acquire_writes_holder_and_release_clears(home: Path) -> None:
    workdir = home / "wd"
    lock = PathLock(home, "local/x-1", workdir, run_id="run-1")
    assert not lock.held
    lock.acquire()
    assert lock.held
    data = json.loads(lock.path.read_text())
    assert data["run_id"] == "run-1"
    assert data["pid"] == os.getpid()
    assert data["workdir"] == str(workdir.resolve())
    assert data["started_at"]
    holder = read_lock_holder(lock.path)
    assert holder is not None and holder.run_id == "run-1" and holder.pid == os.getpid()
    # re-entrant acquire is a no-op
    lock.acquire()
    lock.release()
    assert not lock.held
    assert lock.path.exists()  # never unlinked (no unlink race)
    assert read_lock_holder(lock.path) is None
    lock.release()  # idempotent


def test_second_lock_same_process_conflicts(home: Path) -> None:
    workdir = home / "wd"
    first = PathLock(home, "local/x-1", workdir, run_id="run-1")
    second = PathLock(home, "local/x-1", workdir, run_id="run-2")
    with first:
        with pytest.raises(WorkdirLockedError) as exc:
            second.acquire()
        assert "already locked by run run-1" in str(exc.value)
        assert f"(pid {os.getpid()})" in str(exc.value)
        assert exc.value.run_id == "run-1"
    # released on exit → second can take it
    with second:
        assert second.held
    # a different workdir never conflicts
    other = PathLock(home, "local/x-1", home / "other", run_id="run-3")
    with first, other:
        assert first.held and other.held


def test_context_manager_releases_on_error(home: Path) -> None:
    lock = PathLock(home, "local/x-1", home / "wd", run_id="run-1")
    with pytest.raises(RuntimeError), lock:
        raise RuntimeError("boom")
    assert not lock.held
    PathLock(home, "local/x-1", home / "wd", run_id="run-2").acquire()


_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from rayspec.workspace.lock import PathLock
    home, workdir = Path(sys.argv[1]), Path(sys.argv[2])
    lock = PathLock(home, "local/x-1", workdir, run_id="child-run")
    lock.acquire()
    print("locked", flush=True)
    sys.stdin.readline()  # hold until the parent says so
    lock.release()
    print("released", flush=True)
    """
)


def test_exclusive_across_processes(home: Path) -> None:
    workdir = home / "wd"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(home), str(workdir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        assert proc.stdout.readline().strip() == "locked"
        lock = PathLock(home, "local/x-1", workdir, run_id="parent-run")
        with pytest.raises(WorkdirLockedError) as exc:
            lock.acquire()
        assert exc.value.run_id == "child-run"
        assert exc.value.pid == proc.pid
        assert f"(pid {proc.pid})" in str(exc.value)
        proc.stdin.write("go\n")
        proc.stdin.flush()
        assert proc.stdout.readline().strip() == "released"
        proc.wait(timeout=10)
        lock.acquire()
        assert lock.held
        lock.release()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_lock_auto_released_when_holder_dies(home: Path) -> None:
    workdir = home / "wd"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(home), str(workdir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    proc.kill()
    proc.wait(timeout=10)
    lock = PathLock(home, "local/x-1", workdir, run_id="parent-run")
    lock.acquire()  # the kernel dropped the dead process's flock
    assert lock.held
    lock.release()


def test_blocking_acquire_waits(home: Path) -> None:
    import threading
    import time

    workdir = home / "wd"
    first = PathLock(home, "local/x-1", workdir, run_id="run-1")
    second = PathLock(home, "local/x-1", workdir, run_id="run-2")
    first.acquire()
    done = threading.Event()

    def waiter() -> None:
        second.acquire(blocking=True)
        done.set()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.1)
    assert not done.is_set()
    first.release()
    assert done.wait(timeout=5)
    second.release()


def test_acquire_wraps_os_errors(home: Path) -> None:
    """A lock dir occupied by a file → WorkspaceError, never a raw OSError."""
    from rayspec.workspace.errors import WorkspaceError

    workdir = home / "wd"
    lock = PathLock(home, "local/x-1", workdir, run_id="run-1")
    lock.path.parent.parent.mkdir(parents=True, exist_ok=True)
    lock.path.parent.write_text("not a directory")
    with pytest.raises(WorkspaceError, match="cannot create lock file") as info:
        lock.acquire()
    assert not isinstance(info.value, OSError)
    assert not lock.held


def test_remove_lock_file_unlinks_free_locks_only(home: Path, tmp_path: Path) -> None:
    """``remove_lock_file`` drops a released (or never-written) lock file and leaves a held
    one alone; a missing file is fine."""
    from rayspec.workspace.lock import remove_lock_file

    workdir = tmp_path / "wd"
    workdir.mkdir()
    assert remove_lock_file(home, "local/x", workdir) is False  # nothing there
    PathLock(home, "local/x", workdir, run_id="r").acquire().release()
    path = lock_path(home, "local/x", workdir)
    assert path.exists()
    assert remove_lock_file(home, "local/x", workdir) is True and not path.exists()
    held = PathLock(home, "local/x", workdir, run_id="r2").acquire()
    try:
        assert remove_lock_file(home, "local/x", workdir) is False and path.exists()
    finally:
        held.release()
