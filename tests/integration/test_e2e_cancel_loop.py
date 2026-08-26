"""PRD-07, R5: `rayspec cancel` on a *live* run must be cooperative — a flag the runner notices
at the next step boundary — not a `kill`. Cancelling mid-loop must finalize as `cancelled`
(exit 4), still run `join: always` cleanup steps, and must not interrupt the step attempt that
was already in flight when the flag was written.

None of this exists yet: today `rayspec cancel` on a live run always sends SIGINT, which the
engine treats exactly like a foreground Ctrl-C — the run ends `interrupted` (exit 130), the
in-flight step is marked `interrupted`, and a not-yet-launched `join: always` step never runs
(the whole scope is torn down at once). Every assertion below is written against the documented
cooperative contract and therefore fails against that SIGINT-based behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ._helpers import git_project, invoke

LOOP_WF = """
rayspec: 1
name: cancelloop
steps:
  - id: work
    loop:
      max_iterations: 5
      steps:
        - id: iter
          shell: |
            echo $$ > "$E2E_PIDFILE"
            echo "iter" >> "$E2E_ITERLOG"
            sleep "${E2E_SLEEP:-2}"
  - id: cleanup
    needs: [work]
    join: always
    shell: echo cleanup-done
outputs:
  cleanup: "{{ steps.cleanup.output }}"
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals / process groups")


def _rayspec_bin() -> str:
    """The installed console script (not `-m rayspec.cli.app`): `rayspec cancel`'s pid check
    requires the target's own command line to literally name `rayspec run|resume|approve|reject`
    as whole tokens (see `rayspec.cli._runs_common.pid_is_rayspec_run`)."""
    candidate = Path(sys.executable).with_name("rayspec")
    return str(candidate) if candidate.exists() else "rayspec"


def _wait_for(predicate, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _launch(root: Path, home: Path, pidfile: Path, iterlog: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "RAYSPEC_HOME": str(home),
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
        "E2E_PIDFILE": str(pidfile),
        "E2E_ITERLOG": str(iterlog),
        "E2E_SLEEP": "3",
    }
    return subprocess.Popen(
        [
            _rayspec_bin(),
            "run",
            "cancelloop",
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


def test_cancel_mid_loop_exits_4_with_join_always_cleanup(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"cancelloop": LOOP_WF}, name="cancelloop")
    home = tmp_path / "home"
    home.mkdir()
    pidfile = tmp_path / "shell.pid"
    iterlog = tmp_path / "iters.log"
    proc = _launch(root, home, pidfile, iterlog)
    try:
        _wait_for(
            lambda: pidfile.is_file() and pidfile.read_text().strip().isdigit(),
            timeout=30,
            what="the first loop iteration to start",
        )
        run_dir = next(home.rglob("runs/*"))
        run_id = run_dir.name
        cancelled = invoke(["cancel", run_id, "--yes", "--root", str(root)], home)
        assert cancelled.exit_code == 0, cancelled.output
        stdout, stderr = proc.communicate(timeout=30)
        assert proc.returncode == 4, (proc.returncode, stdout, stderr)
        record = json.loads((run_dir / "run.json").read_text())
        assert record["status"] == "cancelled", record["status"]
        assert record["steps"]["cleanup"]["status"] == "succeeded", record["steps"].get("cleanup")
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cancel_is_checked_at_step_boundaries_not_mid_step(tmp_path: Path) -> None:
    """The iteration that was already running when the cancel flag was written is allowed to
    finish its current attempt — only pending/sibling work stops."""
    root = git_project(tmp_path, {"cancelloop": LOOP_WF}, name="cancelloop")
    home = tmp_path / "home"
    home.mkdir()
    pidfile = tmp_path / "shell.pid"
    iterlog = tmp_path / "iters.log"
    proc = _launch(root, home, pidfile, iterlog)
    try:
        _wait_for(
            lambda: pidfile.is_file() and pidfile.read_text().strip().isdigit(),
            timeout=30,
            what="the first loop iteration to start",
        )
        run_dir = next(home.rglob("runs/*"))
        run_id = run_dir.name
        invoke(["cancel", run_id, "--yes", "--root", str(root)], home)
        proc.communicate(timeout=30)
        record = json.loads((run_dir / "run.json").read_text())
        in_flight = record["steps"].get("work[1]/iter") or record["steps"].get("work[0]/iter")
        assert in_flight is not None, sorted(record["steps"])
        assert in_flight["status"] == "succeeded", in_flight
    finally:
        if proc.poll() is None:
            proc.kill()
