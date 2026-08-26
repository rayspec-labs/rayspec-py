"""PRD-07 `rayspec run --detach`, end to end: R1 (launch returns immediately, exit reflects
launch not outcome), R6 (a gate in a detached run pauses cleanly, never blocks on a TTY) and R7
(the detached child holds the path lock exactly like a foreground run).

``--detach`` does not exist yet: every ``run ... --detach`` invocation below is refused by Click
as an unknown option (exit 2, nothing printed to stdout, no run ever created). Every assertion
below is therefore written against the *documented* behaviour and fails now for that reason —
once ``--detach`` launches a real background child, these exercise the actual contract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ._helpers import git_project, invoke, run_records

RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9]{4}$")

SLOW_WF = """
rayspec: 1
name: slow
steps:
  - id: canary
    shell: |
      echo $$ > "$E2E_PIDFILE"
      sleep "${E2E_SLEEP:-30}"
      echo canary-done
outputs:
  canary: "{{ steps.canary.output }}"
"""

FAILING_WF = """
rayspec: 1
name: failing
steps:
  - id: boom
    shell: exit 1
"""

GATE_WF = """
rayspec: 1
name: gated
steps:
  - id: build
    shell: echo built
  - id: gate
    needs: [build]
    approve: "ship it?"
  - id: ship
    needs: [gate]
    shell: echo shipped
outputs:
  ship: "{{ steps.ship.output }}"
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: --detach uses setsid")


def _wait_for(predicate, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _run_detach(
    root: Path,
    home: Path,
    workflow: str,
    *,
    extra: list[str] | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    full_env = {
        **os.environ,
        "RAYSPEC_HOME": str(home),
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
        **(env or {}),
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "rayspec.cli.app",
            "run",
            workflow,
            "--root",
            str(root),
            "--no-worktree",
            "--detach",
            *(extra or []),
        ],
        cwd=root,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_detach_returns_within_a_second_with_run_id(tmp_path: Path) -> None:
    """A slow canary step is the witness: if launch ever ran the workflow inline instead of
    backgrounding it, this would time out long before returning."""
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    t0 = time.monotonic()
    result = _run_detach(root, home, "slow", env={"E2E_SLEEP": "30"})
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"--detach did not return promptly ({elapsed:.2f}s)"
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    stdout = result.stdout.strip()
    assert RUN_ID_RE.match(stdout), f"stdout is not a bare run id: {stdout!r}"


def test_detach_writes_no_output_to_stdout_besides_run_id(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "slow", env={"E2E_SLEEP": "0"})
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1 and RUN_ID_RE.match(lines[0]), result.stdout


def test_detach_exit_code_reflects_launch_not_outcome(tmp_path: Path) -> None:
    """The workflow itself fails; launch must still report success (exit 0)."""
    root = git_project(tmp_path, {"failing": FAILING_WF}, name="failing")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "failing")
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    run_id = result.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    run_dir = next(home.rglob(f"runs/{run_id}"))
    _wait_for(
        lambda: json.loads((run_dir / "run.json").read_text())["status"] in {"failed", "succeeded"},
        timeout=15,
        what="the detached run to finish",
    )
    record = json.loads((run_dir / "run.json").read_text())
    assert record["status"] == "failed"


def test_detach_survives_parent_exit(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "slow", env={"E2E_SLEEP": "1"})
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    run_id = result.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    # the launcher process is long gone by now; the run must still reach a terminal status
    _wait_for(
        lambda: (
            bool(list(home.rglob(f"runs/{run_id}/run.json")))
            and json.loads(next(home.rglob(f"runs/{run_id}/run.json")).read_text())["status"]
            == "succeeded"
        ),
        timeout=15,
        what="the detached run to complete after the launcher exited",
    )


def test_detached_run_at_gate_reaches_exit_3(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"gated": GATE_WF}, name="gated")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "gated")
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    run_id = result.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    run_dir = next(home.rglob(f"runs/{run_id}"))
    _wait_for(
        lambda: json.loads((run_dir / "run.json").read_text())["status"] == "paused",
        timeout=15,
        what="the detached run to pause at the gate",
    )
    record = json.loads((run_dir / "run.json").read_text())
    assert record.get("pause") is not None, "pause info must be populated"


def test_detached_run_at_gate_is_resumable(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"gated": GATE_WF}, name="gated")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "gated")
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    run_id = result.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    run_dir = next(home.rglob(f"runs/{run_id}"))
    _wait_for(
        lambda: json.loads((run_dir / "run.json").read_text())["status"] == "paused",
        timeout=15,
        what="the detached run to pause at the gate",
    )
    approved = invoke(["approve", run_id, "ship it", "--root", str(root)], home)
    assert approved.exit_code == 0, approved.output
    [rec] = run_records(home)
    assert rec["status"] == "succeeded"


def test_gate_with_no_tty_does_not_hang_when_detached(tmp_path: Path) -> None:
    """Detach guarantees no controlling terminal; a gate must never block reading stdin."""
    root = git_project(tmp_path, {"gated": GATE_WF}, name="gated")
    home = tmp_path / "home"
    home.mkdir()
    t0 = time.monotonic()
    result = _run_detach(root, home, "gated")
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"launch blocked for {elapsed:.2f}s — looks like a hang on stdin"
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)


def test_second_run_same_path_fails_exit_2_naming_first(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    launched = _run_detach(root, home, "slow", env={"E2E_SLEEP": "5"})
    assert launched.returncode == 0, (launched.returncode, launched.stdout, launched.stderr)
    run_id_a = launched.stdout.strip()
    assert RUN_ID_RE.match(run_id_a)
    second = invoke(
        ["run", "slow", "--root", str(root), "--no-worktree", "--no-interactive"],
        home,
        E2E_SLEEP="0",
    )
    assert second.exit_code == 2, second.output
    assert run_id_a in second.output, second.output


def test_lock_released_after_detached_run_ends(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    launched = _run_detach(root, home, "slow", env={"E2E_SLEEP": "1"})
    assert launched.returncode == 0, (launched.returncode, launched.stdout, launched.stderr)
    run_id_a = launched.stdout.strip()
    assert RUN_ID_RE.match(run_id_a)
    run_dir = next(home.rglob(f"runs/{run_id_a}"))
    _wait_for(
        lambda: json.loads((run_dir / "run.json").read_text())["status"] == "succeeded",
        timeout=15,
        what="run A to finish and release the lock",
    )
    third = invoke(
        ["run", "slow", "--root", str(root), "--no-worktree", "--no-interactive"],
        home,
        E2E_SLEEP="0",
    )
    assert third.exit_code == 0, third.output


def _logs_follow(root: Path, home: Path, run_id: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "RAYSPEC_HOME": str(home), "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
    return subprocess.run(
        [sys.executable, "-m", "rayspec.cli.app", "logs", run_id, "--follow", "--root", str(root)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_logs_follow_streams_to_completion_for_a_detached_run(tmp_path: Path) -> None:
    """R3: `rayspec logs -f` against a genuinely backgrounded run must exit once that run
    reaches a terminal status — not hang forever because nothing in-process ever finishes."""
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    launched = _run_detach(root, home, "slow", env={"E2E_SLEEP": "2"})
    assert launched.returncode == 0, (launched.returncode, launched.stdout, launched.stderr)
    run_id = launched.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    followed = _logs_follow(root, home, run_id)
    assert followed.returncode == 0, (followed.returncode, followed.stdout, followed.stderr)
    assert "canary-done" in followed.stdout, followed.stdout


def test_logs_follow_terminates_on_pause_for_a_detached_run(tmp_path: Path) -> None:
    """PAUSED is not RUNNING but also not terminal — `-f` must still exit when a detached run
    stops at a gate, the same as it does for an in-process run."""
    root = git_project(tmp_path, {"gated": GATE_WF}, name="gated")
    home = tmp_path / "home"
    home.mkdir()
    launched = _run_detach(root, home, "gated")
    assert launched.returncode == 0, (launched.returncode, launched.stdout, launched.stderr)
    run_id = launched.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    followed = _logs_follow(root, home, run_id)
    assert followed.returncode == 0, (followed.returncode, followed.stdout, followed.stderr)
