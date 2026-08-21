"""`rayspec cancel <run>`: SIGINT a live run (with confirmation), cancel a paused one."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.schema import RunStatus
from rayspec.store.model import RunRecord

from .conftest import FAILED_ID, PAUSED_ID, Seeded

#: A fake "rayspec run live" process: its command line (as ``ps -o command=`` shows it) names
#: rayspec and the workflow, which is what ``cancel`` verifies before signalling.
FAKE_RAYSPEC = [sys.executable, "-c", "import time; time.sleep(60)  # rayspec run live"]


def test_cancel_paused_run_marks_cancelled(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["cancel", PAUSED_ID[:11], "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "cancelled" in result.output
    run = seeded.store.load(PAUSED_ID)
    assert run.status is RunStatus.CANCELLED and run.pid is None
    assert run.ended_at is not None and run.reason and "cancel" in run.reason
    assert run.pause is not None  # the gate state is kept (a resume asks again)
    events = list(seeded.store.read_events(PAUSED_ID))
    assert events[-1].type.value == "run.finished" and events[-1].data["status"] == "cancelled"
    shown = cli.invoke(app, ["show", PAUSED_ID, "--root", str(seeded.project)])
    assert "cancelled" in shown.output
    again = cli.invoke(app, ["cancel", PAUSED_ID, "--root", str(seeded.project)])
    assert again.exit_code == 2 and "cancelled" in again.output


def test_cancel_rejects_finished_runs(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["cancel", FAILED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 2
    assert "failed" in result.output and "nothing to cancel" in result.output


def _running(seeded: Seeded, run_id: str, pid: int | None, host: str | None) -> RunRecord:
    run = RunRecord(
        run_id=run_id,
        workflow_name="live",
        workflow_path="x.yaml",
        workflow_hash="f" * 64,
        project_slug=seeded.slug,
        project_root=str(seeded.project),
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC),
        pid=pid,
        host=host,
    )
    seeded.store.create(run)
    return run


def test_cancel_live_run_sends_sigint_after_confirmation(cli: CliRunner, seeded: Seeded) -> None:
    proc = subprocess.Popen(FAKE_RAYSPEC)
    try:
        run = _running(seeded, "20260820-150000-live", proc.pid, socket.gethostname())
        declined = cli.invoke(
            app, ["cancel", run.run_id, "--root", str(seeded.project)], input="n\n"
        )
        assert declined.exit_code == 1, declined.output
        assert proc.poll() is None
        result = cli.invoke(app, ["cancel", run.run_id, "--root", str(seeded.project)], input="y\n")
        assert result.exit_code == 0, result.output
        assert "SIGINT" in result.output and str(proc.pid) in result.output
        proc.wait(timeout=10)
        # the record is the engine's to finalize: cancel does not rewrite a live run
        assert seeded.store.load(run.run_id).status is RunStatus.RUNNING
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cancel_live_run_yes_and_json(cli: CliRunner, seeded: Seeded) -> None:
    proc = subprocess.Popen(FAKE_RAYSPEC)
    try:
        run = _running(seeded, "20260820-150100-json", proc.pid, socket.gethostname())
        result = cli.invoke(
            app, ["cancel", run.run_id, "--yes", "--json", "--root", str(seeded.project)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {
            "run_id": run.run_id,
            "action": "signalled",
            "pid": proc.pid,
            "status": "running",
        }
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cancel_running_record_with_dead_pid_marks_cancelled(
    cli: CliRunner, seeded: Seeded
) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    run = _running(seeded, "20260820-150200-dead", proc.pid, socket.gethostname())
    result = cli.invoke(app, ["cancel", run.run_id, "--json", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["action"] == "cancelled" and data["status"] == "cancelled"
    record = seeded.store.load(run.run_id)
    assert record.status is RunStatus.CANCELLED and record.pid is None
    assert record.reason and str(proc.pid) in record.reason


def test_cancel_running_record_on_another_host_needs_force(cli: CliRunner, seeded: Seeded) -> None:
    # a shared RAYSPEC_HOME: the process may well be alive on the other machine
    run = _running(seeded, "20260820-150300-host", 4242, "elsewhere")
    result = cli.invoke(app, ["cancel", run.run_id, "--root", str(seeded.project)])
    assert result.exit_code == 2, result.output
    assert "elsewhere" in result.output and "--force" in result.output
    assert seeded.store.load(run.run_id).status is RunStatus.RUNNING
    forced = cli.invoke(app, ["cancel", run.run_id, "--force", "--root", str(seeded.project)])
    assert forced.exit_code == 0, forced.output
    assert "elsewhere" in forced.output
    record = seeded.store.load(run.run_id)
    assert record.status is RunStatus.CANCELLED and record.pid is None
    # the cancellation line renders run data (host) as plain text, never markup
    odd = _running(seeded, "20260820-150400-mark", 4243, "[/bold] host")
    forced = cli.invoke(app, ["cancel", odd.run_id, "--force", "--root", str(seeded.project)])
    assert forced.exit_code == 0, forced.output
    assert "[/bold] host" in forced.output


def test_cancel_refuses_to_signal_a_pid_that_is_not_a_rayspec_run(
    cli: CliRunner, seeded: Seeded
) -> None:
    """Pid reuse / an edited record must never SIGINT an unrelated process."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        run = _running(seeded, "20260820-150500-pidx", proc.pid, socket.gethostname())
        result = cli.invoke(app, ["cancel", run.run_id, "--yes", "--root", str(seeded.project)])
        assert result.exit_code == 2, result.output
        assert f"pid {proc.pid} is not a rayspec run process (stale record?)" in result.output
        assert "rayspec cancel --mark" in result.output
        assert proc.poll() is None  # untouched
        assert seeded.store.load(run.run_id).status is RunStatus.RUNNING
        # --json: same refusal, no confirmation needed, nothing signalled
        as_json = cli.invoke(app, ["cancel", run.run_id, "--json", "--root", str(seeded.project)])
        assert as_json.exit_code == 2 and proc.poll() is None
        # --mark: the record is finalized without signalling anything
        marked = cli.invoke(
            app, ["cancel", run.run_id, "--mark", "--json", "--root", str(seeded.project)]
        )
        assert marked.exit_code == 0, marked.output
        data = json.loads(marked.output)
        assert data["action"] == "marked" and data["status"] == "cancelled"
        assert data["pid"] is None
        assert proc.poll() is None
        record = seeded.store.load(run.run_id)
        assert record.status is RunStatus.CANCELLED and record.pid is None
        assert record.reason and "--mark" in record.reason and str(proc.pid) in record.reason
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cancel_mark_on_a_live_rayspec_run_does_not_signal(cli: CliRunner, seeded: Seeded) -> None:
    proc = subprocess.Popen(FAKE_RAYSPEC)
    try:
        run = _running(seeded, "20260820-150600-mark", proc.pid, socket.gethostname())
        result = cli.invoke(app, ["cancel", run.run_id, "--mark", "--root", str(seeded.project)])
        assert result.exit_code == 0, result.output
        assert "marked cancelled" in result.output and "SIGINT" not in result.output
        assert proc.poll() is None
        assert seeded.store.load(run.run_id).status is RunStatus.CANCELLED
    finally:
        if proc.poll() is None:
            proc.kill()


def test_pid_verification_helpers(seeded: Seeded) -> None:
    from rayspec.cli import _runs_common as common

    proc = subprocess.Popen(FAKE_RAYSPEC)
    try:
        run = _running(seeded, "20260820-150700-help", proc.pid, socket.gethostname())
        cmdline = common.pid_command_line(proc.pid)
        assert cmdline is not None and "rayspec run live" in cmdline
        assert common.pid_is_rayspec_run(run) is True
        other = _running(seeded, "20260820-150800-othr", proc.pid, socket.gethostname())
        other.workflow_name = "unrelated"
        assert common.pid_is_rayspec_run(other) is False  # names rayspec but not this workflow
        other.workflow_name = "live"
        assert common.pid_is_rayspec_run(other) is True
        assert common.pid_command_line(2**22 + 12345) is None  # no such pid
    finally:
        proc.kill()


def test_pid_check_requires_a_rayspec_command_and_whole_words(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rayspec`` must be followed by run/resume/approve/reject and the run needle
    (id, workflow name, workflow file) must be a whole token — substrings do not count."""
    from rayspec.cli import _runs_common as common

    run = _running(seeded, "20260820-150900-word", 4242, socket.gethostname())  # name "live"
    run.workflow_path = ".rayspec/workflows/live.yaml"
    accepted = [
        "rayspec run live",
        "/home/u/.venv/bin/python /home/u/.venv/bin/rayspec run live --yes",
        "uv run rayspec run ./wf/live.yaml",
        "rayspec run .rayspec/workflows/live.yaml",
        "python -m rayspec run live",
        f"rayspec resume {run.run_id}",
        f"rayspec run --resume {run.run_id} --yes",
        f"rayspec approve {run.run_id} ok",
        "rayspec.exe run live",
    ]
    rejected = [
        "python -c import time; time.sleep(60) rayspec live",  # the probe: no rayspec command
        "rayspec run livewire",  # substring of another workflow name
        "rayspec run olive",
        "rayspec run other --input a=live",  # needle only inside an option value
        "rayspec validate live",  # not an execution command
        "rayspec runs",
        "myrayspec run live",
        "sleep 60",
        f"vim {run.run_id}",
    ]
    for cmdline in accepted:
        monkeypatch.setattr(common, "pid_command_line", lambda pid, c=cmdline: c)
        assert common.pid_is_rayspec_run(run) is True, cmdline
    for cmdline in rejected:
        monkeypatch.setattr(common, "pid_command_line", lambda pid, c=cmdline: c)
        assert common.pid_is_rayspec_run(run) is False, cmdline


def test_cancel_never_signals_an_unrelated_process(cli: CliRunner, seeded: Seeded) -> None:
    """A real ``sleep`` whose pid is recorded as this run's process survives cancel --yes and
    --json; a process that merely mentions rayspec + the workflow name in its argv too."""
    sleeper = subprocess.Popen(["sleep", "60"])
    probe = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "rayspec", "live"]
    )
    try:
        for proc, tag in ((sleeper, "slp"), (probe, "prb")):
            run = _running(seeded, f"20260820-151000-{tag}0", proc.pid, socket.gethostname())
            for extra in (["--yes"], ["--json"], ["--yes", "--json"]):
                result = cli.invoke(
                    app, ["cancel", run.run_id, *extra, "--root", str(seeded.project)]
                )
                assert result.exit_code == 2, (tag, extra, result.output)
                assert "not a rayspec run process" in result.output
                assert "--mark" in result.output
            assert proc.poll() is None, tag  # still alive: nothing was signalled
            assert seeded.store.load(run.run_id).status is RunStatus.RUNNING
    finally:
        for proc in (sleeper, probe):
            if proc.poll() is None:
                proc.kill()
