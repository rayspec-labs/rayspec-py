"""``rayspec cancel`` verifies the recorded pid by its process start time (exact) before the
command-line heuristic; records without ``pid_started_at`` (older runs) use the heuristic only."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli import _runs_common as common
from rayspec.cli.app import app
from rayspec.engine.runner import process_start_time
from rayspec.schema import RunStatus
from rayspec.store.model import RunRecord

from .conftest import Seeded

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process tables")


def _sigint_recorder(marker: Path) -> list[str]:
    """A fake "rayspec run live" process that records a SIGINT by exiting 0 (a SIGINT that is
    NOT handled would end it with 130 / -2); ``sleep 60`` otherwise.

    ``marker`` is created in the statement right after the handler is installed, so its
    existence means "armed" — see :func:`_armed`. It is the trailing positional so the command
    line still reads ``… # rayspec run live <marker>`` and ``cancel``'s heuristic still matches
    both ``rayspec run`` and the ``live`` workflow token.
    """
    return [
        sys.executable,
        "-c",
        "import signal, sys, time\n"
        "signal.signal(signal.SIGINT, lambda *a: sys.exit(0))\n"
        "open(sys.argv[1], 'w').close()\n"
        "time.sleep(60)  # rayspec run live",
        str(marker),
    ]


def _armed(proc: subprocess.Popen[bytes], marker: Path) -> None:
    """Block until ``proc`` has installed its SIGINT handler.

    Between ``Popen`` returning and ``signal.signal`` running (~25 ms: a CPython boot) a SIGINT
    kills the child with the default disposition, so an unsynchronised test would assert an exit
    code of -2 and blame the product. The handshake removes the clock from the test entirely;
    the deadline below is only a guard against a child that never starts, and it fails naming
    the handshake rather than an exit code.
    """
    deadline = time.monotonic() + 30.0
    while not marker.exists():
        if proc.poll() is not None:
            raise AssertionError(f"the recorder exited ({proc.returncode}) before arming")
        if time.monotonic() > deadline:
            raise AssertionError(f"the recorder never armed its SIGINT handler (no {marker})")
        time.sleep(0.001)


def _running(seeded: Seeded, run_id: str, pid: int, *, started_at: str | None) -> RunRecord:
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
        host=socket.gethostname(),
        pid_started_at=started_at,
    )
    seeded.store.create(run)
    return run


def test_cancel_refuses_when_the_start_time_differs(
    cli: CliRunner, seeded: Seeded, tmp_path: Path
) -> None:
    """A pid reused by another run of the SAME workflow passes the command-line heuristic but
    not the exact check: refused, nothing signalled, the --mark hint is printed."""
    proc = subprocess.Popen(_sigint_recorder(tmp_path / "stale.armed"))
    try:
        live = process_start_time(proc.pid)
        assert live is not None
        stale = "Thu Jan  1 00:00:00 2026"
        assert stale != live
        run = _running(seeded, "20260820-160000-stal", proc.pid, started_at=stale)
        for extra in (["--yes"], ["--json"], ["--yes", "--json"]):
            result = cli.invoke(app, ["cancel", run.run_id, *extra, "--root", str(seeded.project)])
            assert result.exit_code == 2, (extra, result.output)
            assert f"pid {proc.pid} is not a rayspec run process (stale record?)" in result.output
            assert "rayspec cancel --mark" in result.output
        assert proc.poll() is None  # untouched
        assert seeded.store.load(run.run_id).status is RunStatus.RUNNING
        marked = cli.invoke(
            app, ["cancel", run.run_id, "--mark", "--json", "--root", str(seeded.project)]
        )
        assert marked.exit_code == 0, marked.output
        assert proc.poll() is None
        assert seeded.store.load(run.run_id).status is RunStatus.CANCELLED
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cancel_signals_when_the_start_time_matches(
    cli: CliRunner, seeded: Seeded, tmp_path: Path
) -> None:
    """PRD-07 R5: a matching pid is not signalled — it is flagged. The process is left running
    (it is the run's own step boundary check that later notices the flag)."""
    marker = tmp_path / "match.armed"
    proc = subprocess.Popen(_sigint_recorder(marker))
    try:
        live = process_start_time(proc.pid)
        assert live is not None
        run = _running(seeded, "20260820-160100-mtch", proc.pid, started_at=live)
        _armed(proc, marker)  # arming is irrelevant to a flag write, but keeps the fixture shared
        result = cli.invoke(app, ["cancel", run.run_id, "--yes", "--root", str(seeded.project)])
        assert result.exit_code == 0, result.output
        assert "cancel requested" in result.output and str(proc.pid) in result.output
        assert (seeded.store.run_dir(run.run_id) / "cancel.json").is_file()
        assert proc.poll() is None  # not signalled: still running
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cancel_with_a_faked_ps_start_time(
    cli: CliRunner, seeded: Seeded, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live start time comes from one probe (``pid_start_time``); a different answer is a
    refusal even though the command line names the workflow."""
    marker = tmp_path / "fake.armed"
    proc = subprocess.Popen(_sigint_recorder(marker))
    try:
        run = _running(seeded, "20260820-160200-fake", proc.pid, started_at="recorded")
        monkeypatch.setattr(common, "pid_start_time", lambda pid: "other")
        refused = cli.invoke(app, ["cancel", run.run_id, "--yes", "--root", str(seeded.project)])
        assert refused.exit_code == 2 and "not a rayspec run process" in refused.output
        assert proc.poll() is None
        monkeypatch.setattr(common, "pid_start_time", lambda pid: None)  # probe failed
        refused = cli.invoke(app, ["cancel", run.run_id, "--yes", "--root", str(seeded.project)])
        assert refused.exit_code == 2 and proc.poll() is None
        monkeypatch.setattr(common, "pid_start_time", lambda pid: "recorded")
        _armed(proc, marker)
        ok = cli.invoke(app, ["cancel", run.run_id, "--yes", "--root", str(seeded.project)])
        assert ok.exit_code == 0, ok.output
        assert (seeded.store.run_dir(run.run_id) / "cancel.json").is_file()
        assert proc.poll() is None  # not signalled: still running
    finally:
        if proc.poll() is None:
            proc.kill()


def test_records_without_pid_started_at_fall_back_to_the_heuristic(
    cli: CliRunner, seeded: Seeded, tmp_path: Path
) -> None:
    marker = tmp_path / "old1.armed"
    rayspec_like = subprocess.Popen(_sigint_recorder(marker))
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        ok_run = _running(seeded, "20260820-160300-old1", rayspec_like.pid, started_at=None)
        bad_run = _running(seeded, "20260820-160400-old2", sleeper.pid, started_at=None)
        refused = cli.invoke(
            app, ["cancel", bad_run.run_id, "--yes", "--root", str(seeded.project)]
        )
        assert refused.exit_code == 2 and "not a rayspec run process" in refused.output
        assert sleeper.poll() is None
        _armed(rayspec_like, marker)  # `sleeper` needs none: it is only ever asserted alive
        ok = cli.invoke(app, ["cancel", ok_run.run_id, "--yes", "--root", str(seeded.project)])
        assert ok.exit_code == 0, ok.output
        assert (seeded.store.run_dir(ok_run.run_id) / "cancel.json").is_file()
        assert rayspec_like.poll() is None  # not signalled: still running
    finally:
        for proc in (rayspec_like, sleeper):
            if proc.poll() is None:
                proc.kill()


def test_start_time_match_still_requires_the_command_line_heuristic(
    cli: CliRunner, seeded: Seeded
) -> None:
    """Both checks must pass: an edited record pointing at ``sleep`` with the right start time
    is still refused (the command line is not a rayspec run)."""
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        live = process_start_time(sleeper.pid)
        run = _running(seeded, "20260820-160500-both", sleeper.pid, started_at=live)
        result = cli.invoke(app, ["cancel", run.run_id, "--yes", "--root", str(seeded.project)])
        assert result.exit_code == 2 and "not a rayspec run process" in result.output
        assert sleeper.poll() is None
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


def test_pid_is_rayspec_run_helper_uses_both_checks(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _running(seeded, "20260820-160600-help", 4242, started_at="T0")
    monkeypatch.setattr(common, "pid_command_line", lambda pid: "rayspec run live")
    monkeypatch.setattr(common, "pid_start_time", lambda pid: "T0")
    assert common.pid_is_rayspec_run(run) is True
    monkeypatch.setattr(common, "pid_start_time", lambda pid: "T1")
    assert common.pid_is_rayspec_run(run) is False
    run.pid_started_at = None  # older record: heuristic only
    assert common.pid_is_rayspec_run(run) is True
    monkeypatch.setattr(common, "pid_command_line", lambda pid: "sleep 60")
    assert common.pid_is_rayspec_run(run) is False
    run.pid_started_at = "T1"
    assert common.pid_is_rayspec_run(run) is False


def test_pid_is_rayspec_run_recognises_the_module_launch_shape(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD-07 D3: a detached child runs as ``python -m rayspec.cli.app run <wf>`` — the token
    ``rayspec`` is followed by ``.cli.app``, which the command matcher must accept, or
    ``rayspec cancel`` refuses every detached run."""
    run = _running(seeded, "20260820-160700-mod", 4242, started_at="T0")
    monkeypatch.setattr(common, "pid_start_time", lambda pid: "T0")
    for cmdline in (
        "/usr/bin/python3 -m rayspec.cli.app run live --detached-child /x/runs/20260820-160700-mod",
        "python -m rayspec.cli run live",
        "python -m rayspec run live",
        "/venv/bin/rayspec resume live",
    ):
        monkeypatch.setattr(common, "pid_command_line", lambda pid, c=cmdline: c)
        assert common.pid_is_rayspec_run(run) is True, cmdline
    # still rejects a look-alike that is not a rayspec execution command
    monkeypatch.setattr(
        common, "pid_command_line", lambda pid: "python -m rayspecx.cli.app run live"
    )
    assert common.pid_is_rayspec_run(run) is False
