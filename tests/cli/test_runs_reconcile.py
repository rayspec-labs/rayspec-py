# SPDX-License-Identifier: Apache-2.0
"""PRD-07, R4: `rayspec runs` (and every other read path: `show`, ...) must reconcile a stored
``running`` status against reality — a dead pid, or a live pid whose heartbeat has gone stale,
is reported *and persisted* as ``interrupted``. None of this exists yet: today the CLI echoes
``run.json``'s stored status verbatim, however long dead the process behind ``pid`` is.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.schema import RunStatus
from rayspec.store.model import RunRecord

from .conftest import Seeded


def _running(seeded: Seeded, run_id: str, *, pid: int, heartbeat_at=None) -> RunRecord:
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
        heartbeat_at=heartbeat_at,
    )
    seeded.store.create(run)
    return run


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


def test_runs_reports_dead_pid_as_interrupted(cli: CliRunner, seeded: Seeded) -> None:
    run = _running(seeded, "20260827-100000-dead", pid=_dead_pid())
    result = cli.invoke(app, ["runs", "--root", str(seeded.project), "--json"])
    assert result.exit_code == 0, result.output
    rows = {row["run_id"]: row for row in json.loads(result.output)}
    assert rows[run.run_id]["status"] == "interrupted", rows[run.run_id]


def test_runs_corrects_stored_status_on_read(cli: CliRunner, seeded: Seeded) -> None:
    run = _running(seeded, "20260827-100100-dead", pid=_dead_pid())
    result = cli.invoke(app, ["runs", "--root", str(seeded.project), "--json"])
    assert result.exit_code == 0, result.output
    reloaded = seeded.store.load(run.run_id)
    assert reloaded.status is RunStatus.INTERRUPTED, reloaded.status


def test_runs_reports_stale_heartbeat_as_interrupted_even_with_live_pid(
    cli: CliRunner, seeded: Seeded
) -> None:
    """A live pid alone is not enough: a heartbeat unrefreshed well past the staleness threshold
    means the process is stuck or the record is bogus, not that the run is progressing."""
    stale = datetime.now(UTC) - timedelta(hours=1)
    run = _running(seeded, "20260827-100200-stale", pid=os.getpid(), heartbeat_at=stale)
    result = cli.invoke(app, ["runs", "--root", str(seeded.project), "--json"])
    assert result.exit_code == 0, result.output
    rows = {row["run_id"]: row for row in json.loads(result.output)}
    assert rows[run.run_id]["status"] == "interrupted", rows[run.run_id]


def test_runs_does_not_flag_fresh_heartbeat_as_interrupted(cli: CliRunner, seeded: Seeded) -> None:
    """No false positive on an actually-live detached run — a run row must expose the
    heartbeat it reconciles against, and a fresh one must not be reported as interrupted."""
    fresh = datetime.now(UTC)
    run = _running(seeded, "20260827-100300-fresh", pid=os.getpid(), heartbeat_at=fresh)
    result = cli.invoke(app, ["runs", "--root", str(seeded.project), "--json"])
    assert result.exit_code == 0, result.output
    rows = {row["run_id"]: row for row in json.loads(result.output)}
    assert "heartbeat_at" in rows[run.run_id], "the row does not expose the heartbeat it uses"
    assert rows[run.run_id]["status"] == "running", rows[run.run_id]


def test_show_also_reconciles_status(cli: CliRunner, seeded: Seeded) -> None:
    """The same reconciliation applies to every read path, not only the `runs` listing."""
    run = _running(seeded, "20260827-100400-show", pid=_dead_pid())
    result = cli.invoke(app, ["show", run.run_id, "--root", str(seeded.project), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "interrupted", data


def test_reconcile_reports_a_reused_pid_as_interrupted(cli: CliRunner, seeded: Seeded) -> None:
    """A running record whose pid now belongs to a different process (start time differs) is
    interrupted — the case a bare pid check would miss."""

    run = _running(
        seeded, "20260827-100900-reuse", pid=os.getpid(), heartbeat_at=datetime.now(UTC)
    )
    run.pid_started_at = "a-start-time-that-is-not-this-process"
    seeded.store.save(run)
    result = cli.invoke(app, ["runs", "--root", str(seeded.project), "--json"])
    assert result.exit_code == 0, result.output
    rows = {row["run_id"]: row for row in json.loads(result.output)}
    assert rows[run.run_id]["status"] == "interrupted"


def test_reconcile_never_tracebacks_on_a_read_only_store(cli: CliRunner, seeded: Seeded) -> None:
    """PRD-07 D9: the correction persists best-effort — a run dir the process cannot write must
    not turn the read-only ``runs`` into a crash; the status is still corrected in the output."""
    import stat

    run = _running(seeded, "20260827-101000-ro", pid=_dead_pid())
    run_dir = seeded.store.run_dir(run.run_id)
    original = run_dir.stat().st_mode
    run_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x: reads work, writes fail
    try:
        result = cli.invoke(app, ["runs", "--root", str(seeded.project), "--json"])
    finally:
        run_dir.chmod(original)
    assert result.exit_code == 0, result.output
    rows = {row["run_id"]: row for row in json.loads(result.output)}
    assert rows[run.run_id]["status"] == "interrupted"
