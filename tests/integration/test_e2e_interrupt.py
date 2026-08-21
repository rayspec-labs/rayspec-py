"""Interrupt + resume end to end: SIGINT a real ``rayspec run`` subprocess mid-step, then resume.

The run is a separate Python process (the CLI's own SIGINT handler, not pytest's); the running
shell step lives in its own process group (``start_new_session``), so after the CLI exits the
test checks that nothing of that group survived (no orphan shells — the same mechanism covers
``claude``/``codex`` subprocesses, which the stub provider never spawns).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ._helpers import git_project, invoke, jsonl, run_records

SLOW_WF = """
rayspec: 1
name: slow
steps:
  - id: first
    shell: echo first-done
  - id: slow
    needs: [first]
    shell: |
      echo $$ > "$E2E_PIDFILE"
      sleep "${E2E_SLEEP:-0}"
      echo slow-done
  - id: after
    needs: [slow]
    agent: { provider: stub }
    prompt: "after {{ steps.slow.output }}"
outputs:
  first: "{{ steps.first.output }}"
  slow: "{{ steps.slow.output }}"
  after: "{{ steps.after.output }}"
"""


def _pgid_members(pgid: int) -> list[str]:
    out = subprocess.run(["ps", "-eo", "pid=,pgid=,command="], capture_output=True, text=True)
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) == pgid:
            rows.append(line.strip())
    return rows


def _wait_for(predicate, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals / process groups")
def test_sigint_interrupts_the_run_without_orphans_and_resume_completes(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    pidfile = tmp_path / "shell.pid"
    env = {
        **os.environ,
        "RAYSPEC_HOME": str(home),
        "NO_COLOR": "1",
        "E2E_PIDFILE": str(pidfile),
        "E2E_SLEEP": "60",
        "PYTHONUNBUFFERED": "1",
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
            "--json",
        ],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_for(
            lambda: pidfile.is_file() and pidfile.read_text().strip().isdigit(),
            timeout=30,
            what="the slow shell step to start",
        )
        shell_pid = int(pidfile.read_text().strip())
        assert _pgid_members(shell_pid), "the shell step leads its own process group"
        time.sleep(0.2)
        t0 = time.monotonic()
        os.kill(proc.pid, signal.SIGINT)  # Ctrl-C to the CLI only (the shell has its own session)
        stdout, stderr = proc.communicate(timeout=30)
        elapsed = time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130, (proc.returncode, stdout, stderr)
    assert elapsed < 15, f"the run did not stop promptly after SIGINT ({elapsed:.1f}s)"
    # no orphan: the shell step's process group is gone (SIGTERM → SIGKILL by the engine)
    _wait_for(lambda: not _pgid_members(shell_pid), timeout=5, what="the shell group to vanish")
    with pytest.raises(ProcessLookupError):
        os.kill(shell_pid, 0)

    lines = jsonl(stdout)
    summary = lines[-1]
    assert summary["status"] == "interrupted" and summary["exit_code"] == 130, summary
    assert lines[-2]["type"] == "run.finished" and lines[-2]["data"]["status"] == "interrupted"
    [rec] = run_records(home)
    run_id = rec["run_id"]
    assert rec["status"] == "interrupted" and rec["pid"] is None
    assert rec["steps"]["first"]["status"] == "succeeded"
    assert rec["steps"]["slow"]["status"] == "interrupted"
    assert rec["steps"].get("after", {}).get("status", "skipped") == "skipped"

    # runs/show see the interrupted run
    res = invoke(["runs", "--root", str(root), "--json"], home)
    assert res.exit_code == 0 and json.loads(res.stdout)[0]["status"] == "interrupted"

    # resume: `first` is replayed, `slow` re-runs (fast now), `after` runs
    res = invoke(
        ["run", "slow", "--root", str(root), "--resume", run_id[:9], "--no-interactive", "--json"],
        home,
        E2E_SLEEP="0",
        E2E_PIDFILE=str(pidfile),
    )
    assert res.exit_code == 0, res.output
    lines = jsonl(res.stdout)
    summary = lines[-1]
    assert summary["run_id"] == run_id and summary["status"] == "succeeded"
    assert summary["outputs"] == {
        "first": "first-done",
        "slow": "slow-done",
        "after": "[stub] after slow-done",
    }
    assert lines[0]["type"] == "run.resumed"
    finished = [e for e in lines[:-1] if e["type"] == "step.finished"]
    by_path = {e["step_path"]: e["data"] for e in finished}
    assert by_path["first"].get("reused") is True
    assert by_path["slow"]["status"] == "succeeded" and not by_path["slow"].get("reused")
    assert by_path["after"]["status"] == "succeeded"
    [rec] = run_records(home)
    assert rec["status"] == "succeeded" and rec["resume_count"] == 1
    assert rec["steps"]["slow"]["attempts"] == 2
    # stdout.log keeps both attempts of the interrupted shell step
    run_dir = next(home.rglob(f"runs/{run_id}"))
    log = (run_dir / "steps" / "slow" / "stdout.log").read_text()
    assert "--- attempt 2 ---" in log and "slow-done" in log

    # `rayspec resume` of a finished run is refused (exit 2) unless --force …
    res = invoke(["resume", run_id, "--root", str(root), "--no-interactive"], home)
    assert res.exit_code == 2, res.output
    assert "already succeeded" in res.output and "nothing to resume" in res.output, res.output
    # … and with --force it replays every step from the cache: exit 0, nothing re-runs
    res = invoke(["resume", run_id, "--root", str(root), "--no-interactive", "--force"], home)
    assert res.exit_code == 0, res.output
    assert "reused 3 step(s) from the previous attempt" in res.output, res.output
    assert f"run {run_id} succeeded" in res.output
    [rec] = run_records(home)
    assert rec["status"] == "succeeded" and rec["resume_count"] == 2
    assert rec["steps"]["slow"]["attempts"] == 2, "the replay did not re-run the shell step"
