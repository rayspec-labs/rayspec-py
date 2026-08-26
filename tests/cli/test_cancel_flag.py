# SPDX-License-Identifier: Apache-2.0
"""PRD-07, R5: `rayspec cancel` on a live run is cooperative — it writes a marker file the
runner checks at step boundaries, rather than signalling the process. Today `cancel` always
sends SIGINT to a verified live rayspec process; no marker file is ever written.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from rayspec.cli import _runs_common as common
from rayspec.cli.app import app
from rayspec.schema import RunStatus
from rayspec.store.model import RunRecord

from .conftest import Seeded

#: A fake "rayspec run live" process — its command line names rayspec + the workflow, which is
#: what `cancel` verifies before acting (see `tests/cli/test_cancel_cmd.py`).
FAKE_RAYSPEC = [sys.executable, "-c", "import time; time.sleep(60)  # rayspec run live"]


def _running(seeded: Seeded, run_id: str, pid: int) -> RunRecord:
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
    )
    seeded.store.create(run)
    return run


def test_cancel_writes_flag_file_not_signal_for_live_run(
    cli: CliRunner, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = subprocess.Popen(FAKE_RAYSPEC)
    signalled: list[int] = []
    monkeypatch.setattr(common, "interrupt_pid", lambda pid: signalled.append(pid))
    try:
        run = _running(seeded, "20260827-110000-live", proc.pid)
        result = cli.invoke(app, ["cancel", run.run_id, "--yes", "--root", str(seeded.project)])
        assert result.exit_code == 0, result.output
        run_dir = seeded.store.run_dir(run.run_id)
        assert (run_dir / "cancel.json").exists(), sorted(p.name for p in run_dir.iterdir())
        assert signalled == [], "cancel must not SIGINT a live run — it writes a flag instead"
        assert proc.poll() is None, "the fake live process must not be touched by cancel"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cancel_flag_needs_no_tty(
    cli: CliRunner, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rayspec cancel` on a detached run must not require a terminal — `--yes`/`--json` waive
    the confirmation prompt exactly as they do today for the SIGINT path."""
    proc = subprocess.Popen(FAKE_RAYSPEC)
    monkeypatch.setattr(common, "interrupt_pid", lambda pid: None)
    try:
        run = _running(seeded, "20260827-110100-live", proc.pid)
        result = cli.invoke(
            app, ["cancel", run.run_id, "--json", "--root", str(seeded.project)], input=""
        )
        assert result.exit_code == 0, result.output
        run_dir = seeded.store.run_dir(run.run_id)
        assert (run_dir / "cancel.json").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
