"""PRD-07 `rayspec run --detach`, end to end.

R1: launch backgrounds the workflow and returns the bare run id, exit 0 reflecting the *launch*
not the run's outcome. R3: `logs -f [--exit-code]` streams a detached run to a terminal status
and can report its code. R5: `cancel`/`cancel --now` stop a real detached child. R6: a gate in a
detached run pauses cleanly (no controlling terminal, never blocks on stdin). R7: the detached
child holds the path lock exactly like a foreground run.

These drive real subprocesses (a genuinely backgrounded `setsid` child), so they assert on
ordering and recorded state — a run's status reaching a value, a lock being taken — rather than
wall-clock deadlines, which flake under CI load. Slow witnesses are cleaned up with `cancel
--now` in a finally.
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
      echo $$ > "${E2E_PIDFILE:-/dev/null}"
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

# a loop that checks the cooperative cancel flag between iterations (a single long sleep never
# would): each tick is short, so `cancel` (the flag) stops it within about one tick.
COOP_WF = """
rayspec: 1
name: coop
steps:
  - id: work
    loop:
      max_iterations: 60
      steps:
        - id: tick
          shell: sleep 0.5
"""

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: --detach uses setsid")


def _wait_for(predicate, *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _run_dir(home: Path, run_id: str) -> Path:
    return next(home.rglob(f"runs/{run_id}"))


def _status(home: Path, run_id: str) -> str:
    return json.loads((_run_dir(home, run_id) / "run.json").read_text())["status"]


def _wait_status(home: Path, run_id: str, wanted: set[str], *, timeout: float = 20) -> str:
    _wait_for(
        lambda: (
            bool(list(home.rglob(f"runs/{run_id}/run.json"))) and _status(home, run_id) in wanted
        ),
        timeout=timeout,
        what=f"run {run_id} to reach {wanted}",
    )
    return _status(home, run_id)


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
        timeout=15,
    )


def _cancel_now(root: Path, home: Path, run_id: str) -> None:
    """Best-effort teardown for a slow witness — SIGINT the detached group so nothing is left
    sleeping for 30 s after the test."""
    invoke(["cancel", run_id, "--now", "--yes", "--root", str(root)], home)


def test_detach_backgrounds_the_run_and_returns_a_run_id(tmp_path: Path) -> None:
    """The witness sleeps far longer than the launcher's own timeout: if launch ran the
    workflow inline the subprocess would hit its 15 s timeout, never returning a run id. That it
    returns promptly with the run still RUNNING is the ordering proof it backgrounded."""
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "slow", env={"E2E_SLEEP": "30"})
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    run_id = result.stdout.strip()
    assert RUN_ID_RE.match(run_id), f"stdout is not a bare run id: {run_id!r}"
    try:
        # the run outlives the launcher: right after launch it is still running, not finished
        assert _wait_status(home, run_id, {"running"}, timeout=10) == "running"
    finally:
        _cancel_now(root, home, run_id)


def test_detach_writes_no_output_to_stdout_besides_run_id(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "slow", env={"E2E_SLEEP": "0"})
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1 and RUN_ID_RE.match(lines[0]), result.stdout


def test_detach_json_output_is_one_launch_object(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "slow", extra=["--json"], env={"E2E_SLEEP": "0"})
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, result.stdout
    obj = json.loads(lines[0])
    assert RUN_ID_RE.match(obj["run_id"]) and obj["started"] is True
    assert obj["pid"] and obj["run_dir"] and obj["launch_log"].endswith("detach-launch.log")


def test_detach_exit_code_reflects_launch_not_outcome(tmp_path: Path) -> None:
    """The workflow itself fails; launch must still report success (exit 0)."""
    root = git_project(tmp_path, {"failing": FAILING_WF}, name="failing")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "failing")
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    run_id = result.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    assert _wait_status(home, run_id, {"failed", "succeeded"}) == "failed"


def test_detach_survives_parent_exit(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "slow", env={"E2E_SLEEP": "1"})
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    run_id = result.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    # the launcher process is long gone by now; the run must still reach a terminal status
    assert _wait_status(home, run_id, {"succeeded"}) == "succeeded"


def test_no_stale_launch_artifacts_after_a_clean_launch(tmp_path: Path) -> None:
    """The handshake file is transient plumbing; a launched run keeps its launch log (a record
    of the child's boot) but the run directory is the real one, not a sibling."""
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "slow", env={"E2E_SLEEP": "0"})
    run_id = result.stdout.strip()
    _wait_status(home, run_id, {"succeeded"})
    run_dir = _run_dir(home, run_id)
    assert (run_dir / "detach-launch.log").exists(), "the launch log lives inside the run dir"
    # no sibling <run_id>.detach-launch.log next to the run directory (the old wrong location)
    assert not (run_dir.parent / f"{run_id}.detach-launch.log").exists()


def test_detached_run_at_gate_reaches_paused(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"gated": GATE_WF}, name="gated")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "gated")
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    run_id = result.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    assert _wait_status(home, run_id, {"paused"}) == "paused"
    record = json.loads((_run_dir(home, run_id) / "run.json").read_text())
    assert record.get("pause") is not None, "pause info must be populated"


def test_detached_run_at_gate_is_resumable(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"gated": GATE_WF}, name="gated")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "gated")
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    run_id = result.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    _wait_status(home, run_id, {"paused"})
    approved = invoke(["approve", run_id, "ship it", "--root", str(root)], home)
    assert approved.exit_code == 0, approved.output
    [rec] = run_records(home)
    assert rec["status"] == "succeeded"


def test_gate_with_no_tty_does_not_hang_when_detached(tmp_path: Path) -> None:
    """Detach guarantees no controlling terminal; a gate must never block reading stdin. The
    witness is that launch returns at all (its 15 s subprocess timeout would fire on a hang)."""
    root = git_project(tmp_path, {"gated": GATE_WF}, name="gated")
    home = tmp_path / "home"
    home.mkdir()
    result = _run_detach(root, home, "gated")
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert _wait_status(home, run_id=result.stdout.strip(), wanted={"paused"}) == "paused"


def test_second_run_same_path_fails_exit_2_naming_first(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    launched = _run_detach(root, home, "slow", env={"E2E_SLEEP": "30"})
    assert launched.returncode == 0, (launched.returncode, launched.stdout, launched.stderr)
    run_id_a = launched.stdout.strip()
    assert RUN_ID_RE.match(run_id_a)
    try:
        _wait_status(home, run_id_a, {"running"}, timeout=10)
        second = invoke(
            ["run", "slow", "--root", str(root), "--no-worktree", "--no-interactive"],
            home,
            E2E_SLEEP="0",
        )
        assert second.exit_code == 2, second.output
        assert run_id_a in second.output, second.output
    finally:
        _cancel_now(root, home, run_id_a)


def test_second_detached_run_same_path_fails_exit_2(tmp_path: Path) -> None:
    """R7 both directions: the launcher's own preflight lock check refuses a second *detached*
    in-place run before backgrounding anything, naming the holder."""
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    first = _run_detach(root, home, "slow", env={"E2E_SLEEP": "30"})
    run_id_a = first.stdout.strip()
    assert RUN_ID_RE.match(run_id_a)
    try:
        _wait_status(home, run_id_a, {"running"}, timeout=10)
        second = _run_detach(root, home, "slow", env={"E2E_SLEEP": "0"})
        assert second.returncode == 2, (second.returncode, second.stdout, second.stderr)
        assert run_id_a in (second.stdout + second.stderr)
    finally:
        _cancel_now(root, home, run_id_a)


def test_lock_released_after_detached_run_ends(tmp_path: Path) -> None:
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    launched = _run_detach(root, home, "slow", env={"E2E_SLEEP": "1"})
    assert launched.returncode == 0, (launched.returncode, launched.stdout, launched.stderr)
    run_id_a = launched.stdout.strip()
    assert RUN_ID_RE.match(run_id_a)
    _wait_status(home, run_id_a, {"succeeded"})
    third = invoke(
        ["run", "slow", "--root", str(root), "--no-worktree", "--no-interactive"],
        home,
        E2E_SLEEP="0",
    )
    assert third.exit_code == 0, third.output


def test_cancel_now_stops_a_detached_run(tmp_path: Path) -> None:
    """R5: `cancel --now` SIGINTs the detached child's group; a single long sleep is interrupted
    at once (the cooperative flag alone could not break into a sleep)."""
    root = git_project(tmp_path, {"slow": SLOW_WF}, name="slow")
    home = tmp_path / "home"
    home.mkdir()
    launched = _run_detach(root, home, "slow", env={"E2E_SLEEP": "30"})
    run_id = launched.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    _wait_status(home, run_id, {"running"}, timeout=10)
    cancelled = invoke(["cancel", run_id, "--now", "--yes", "--root", str(root)], home)
    assert cancelled.exit_code == 0, cancelled.output
    assert _wait_status(home, run_id, {"cancelled", "interrupted"}) in {"cancelled", "interrupted"}


def test_cancel_flag_stops_a_detached_loop_between_iterations(tmp_path: Path) -> None:
    """R5: the cooperative flag (plain `cancel`) is honoured at a step boundary — a bounded loop
    of short ticks stops within about one tick, recorded cancelled."""
    root = git_project(tmp_path, {"coop": COOP_WF}, name="coop")
    home = tmp_path / "home"
    home.mkdir()
    launched = _run_detach(root, home, "coop")
    run_id = launched.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    _wait_status(home, run_id, {"running"}, timeout=10)
    cancelled = invoke(["cancel", run_id, "--yes", "--root", str(root)], home)
    assert cancelled.exit_code == 0, cancelled.output
    assert _wait_status(home, run_id, {"cancelled"}, timeout=15) == "cancelled"


def _logs_follow(
    root: Path, home: Path, run_id: str, *, exit_code: bool = False
) -> subprocess.CompletedProcess:
    env = {**os.environ, "RAYSPEC_HOME": str(home), "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
    args = [
        sys.executable,
        "-m",
        "rayspec.cli.app",
        "logs",
        run_id,
        "--follow",
        "--root",
        str(root),
    ]
    if exit_code:
        args.append("--exit-code")
    return subprocess.run(args, cwd=root, env=env, capture_output=True, text=True, timeout=30)


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
    assert "succeeded" in followed.stdout, followed.stdout
    # the step's raw stdout is still retrievable (it is just not inlined in the follow tree)
    streamed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rayspec.cli.app",
            "logs",
            run_id,
            "--step",
            "canary",
            "--root",
            str(root),
        ],
        cwd=root,
        env={**os.environ, "RAYSPEC_HOME": str(home), "NO_COLOR": "1"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "canary-done" in streamed.stdout, (streamed.stdout, streamed.stderr)


def test_logs_follow_exit_code_reflects_a_failed_detached_run(tmp_path: Path) -> None:
    """R3: `logs -f --exit-code` lets a detached run be waited on and its outcome learned — a
    failed run exits the follower with 1."""
    root = git_project(tmp_path, {"failing": FAILING_WF}, name="failing")
    home = tmp_path / "home"
    home.mkdir()
    launched = _run_detach(root, home, "failing")
    run_id = launched.stdout.strip()
    assert RUN_ID_RE.match(run_id)
    followed = _logs_follow(root, home, run_id, exit_code=True)
    assert followed.returncode == 1, (followed.returncode, followed.stdout, followed.stderr)


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
