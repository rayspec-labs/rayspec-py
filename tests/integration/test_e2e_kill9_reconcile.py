"""PRD-07, R4 acceptance criterion, literally: ``kill -9`` a live run's process, then
``rayspec runs`` must report it ``interrupted`` — not ``running`` forever. No detach is needed to
exercise this: an ordinary backgrounded ``rayspec run`` subprocess, killed uncleanly, leaves
exactly the stale ``running`` record this reconciliation must catch. Nothing corrects it today.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ._helpers import git_project, invoke

SLOW_WF = """
rayspec: 1
name: slow
steps:
  - id: slow
    shell: |
      echo $$ > "$E2E_PIDFILE"
      sleep "${E2E_SLEEP:-60}"
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals (SIGKILL)")


def _wait_for(predicate, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_kill9_then_runs_reports_interrupted(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    pidfile = tmp_path / "shell.pid"
    env = {
        **os.environ,
        "RAYSPEC_HOME": str(home),
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
        "E2E_PIDFILE": str(pidfile),
        "E2E_SLEEP": "60",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rayspec.cli.app",
            "run",
            "slow",
            "--root",
            str(root),
            "--no-worktree",
            "--no-interactive",
        ],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(
            lambda: pidfile.is_file() and pidfile.read_text().strip().isdigit(),
            timeout=30,
            what="the shell step to start",
        )
        os.kill(proc.pid, signal.SIGKILL)  # no cleanup, no chance to finalize the record
        proc.wait(timeout=10)
        run_dir = next(home.rglob("runs/*"))
        assert json.loads((run_dir / "run.json").read_text())["status"] == "running"

        def _reported_interrupted() -> bool:
            res = invoke(["runs", "--root", str(root), "--json"], home)
            rows = json.loads(res.stdout)
            return bool(rows) and rows[0]["status"] == "interrupted"

        _wait_for(_reported_interrupted, timeout=10, what="`rayspec runs` to reconcile the pid")
        record = json.loads((run_dir / "run.json").read_text())
        assert record["status"] == "interrupted", record
    finally:
        if proc.poll() is None:
            proc.kill()
        # SIGKILL of the rayspec parent orphans the shell step's own group (the engine runs it
        # with start_new_session, so pgid == the pid the step wrote to the pidfile). Reap that
        # group so a stray `sleep` does not linger after the test.
        with contextlib.suppress(Exception):
            os.killpg(int(pidfile.read_text().strip()), signal.SIGKILL)
