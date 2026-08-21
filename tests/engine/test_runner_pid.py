"""The engine records the launching process's start time next to ``pid`` in ``run.json``
(``pid_started_at``) so ``rayspec cancel`` can verify the pid exactly; refreshed on resume."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from rayspec.engine import runner as runner_mod
from rayspec.engine.context import RunOptions
from rayspec.engine.runner import process_start_time
from rayspec.schema import RunStatus
from rayspec.store.model import Decision, RunRecord

from .conftest import Harness

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process tables")


def test_process_start_time_of_this_process_matches_ps() -> None:
    """The probe is ``ps -o lstart=`` run under ``LC_ALL=C TZ=UTC`` (a fixed environment, so the
    string does not depend on the caller's locale or timezone)."""
    ours = process_start_time(os.getpid())
    assert ours is not None and ours.strip() == ours and ours
    expected = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
    ).stdout.strip()
    assert ours == expected


def test_process_start_time_is_independent_of_tz_and_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of the engine records the string under the launching shell's environment and
    ``rayspec cancel`` re-probes under the cancelling shell's — a run launched from cron/CI
    (``LC_ALL=C``, ``TZ=UTC``, ``LANG`` unset) must still match when cancelled from an
    interactive shell in another timezone / locale."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        monkeypatch.setenv("TZ", "UTC")
        monkeypatch.setenv("LC_ALL", "C")
        monkeypatch.delenv("LANG", raising=False)
        monkeypatch.delenv("LC_TIME", raising=False)
        recorded = process_start_time(proc.pid)
        assert recorded
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        monkeypatch.setenv("LANG", "de_DE.UTF-8")
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        monkeypatch.setenv("LC_TIME", "de_DE.UTF-8")
        assert process_start_time(proc.pid) == recorded
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        monkeypatch.delenv("LC_ALL", raising=False)
        assert process_start_time(proc.pid) == recorded
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_process_start_time_unknown_for_dead_or_invalid_pids() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    assert process_start_time(proc.pid) is None
    assert process_start_time(0) is None
    assert process_start_time(-1) is None


def test_proc_stat_fallback_parses_field_22(tmp_path: Path) -> None:
    """Linux without ``ps``: field 22 of ``/proc/<pid>/stat`` — the comm field may contain spaces
    and parentheses, so the split happens after the LAST ``)``."""
    stat = tmp_path / "stat"
    fields = ["S", *(str(n) for n in range(4, 22)), "4242424", "99"]  # 3, 4..21, 22, 23
    stat.write_text("4711 (ray (spec) run) " + " ".join(fields) + "\n")
    assert runner_mod._proc_starttime(4711, proc_root=tmp_path.parent) is None  # no such dir
    proc_root = tmp_path / "proc"
    (proc_root / "4711").mkdir(parents=True)
    (proc_root / "4711" / "stat").write_text(stat.read_text())
    assert runner_mod._proc_starttime(4711, proc_root=proc_root) == "4242424"
    (proc_root / "4711" / "stat").write_text("garbage\n")
    assert runner_mod._proc_starttime(4711, proc_root=proc_root) is None


def test_process_start_time_falls_back_to_proc_when_ps_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc_root = tmp_path / "proc"
    (proc_root / "77").mkdir(parents=True)
    (proc_root / "77" / "stat").write_text("77 (x) S " + " ".join(["1"] * 18) + " 123456 0\n")
    monkeypatch.setattr(runner_mod, "_PROC_ROOT", proc_root)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))  # no ps anywhere
    (tmp_path / "empty-bin").mkdir()
    assert process_start_time(77) == "123456"
    assert process_start_time(78) is None


def test_process_start_time_falls_back_to_proc_when_ps_cannot_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of a ``ps`` that exists but does not know ``-o lstart`` (busybox/Alpine) exits
    non-zero — ``/proc`` must be consulted then too, not only when ``ps`` is missing entirely;
    a dead pid is still ``None`` (absent from ``/proc`` as well)."""
    proc_root = tmp_path / "proc"
    (proc_root / "77").mkdir(parents=True)
    (proc_root / "77" / "stat").write_text("77 (x) S " + " ".join(["1"] * 18) + " 123456 0\n")
    monkeypatch.setattr(runner_mod, "_PROC_ROOT", proc_root)
    shim_bin = tmp_path / "shim-bin"
    shim_bin.mkdir()
    ps = shim_bin / "ps"
    ps.write_text("#!/bin/sh\necho 'ps: unrecognized option: o' >&2\nexit 1\n")
    ps.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim_bin))
    assert process_start_time(77) == "123456"
    assert process_start_time(78) is None
    # a ps that hangs (timeout) or crashes is treated the same way
    ps.write_text("#!/bin/sh\nsleep 5\n")
    assert process_start_time(77, timeout_s=0.2) == "123456"
    assert process_start_time(78, timeout_s=0.2) is None


def wf(steps: str) -> str:
    return f"rayspec: 1\nname: t\nsteps:\n{steps}"


@pytest.mark.anyio
async def test_run_records_pid_started_at_and_refreshes_it_on_resume(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: echo built}
  - {id: gate, needs: [a], approve: "ship?"}
  - {id: ship, needs: [gate], shell: "echo shipped"}
"""),
    )
    result = await harness.run("t", options=RunOptions(interactive=False))
    assert result.status is RunStatus.PAUSED
    run = harness.record(result.run_id)
    assert run.pid == os.getpid()
    assert run.pid_started_at == process_start_time(os.getpid())
    # a resume runs in a new process: the recorded start time must follow the new pid
    monkeypatch.setattr(runner_mod, "process_start_time", lambda pid: "Thu Aug 20 12:00:00 2026")
    run.pause.decision = Decision(approved=True, comment="go", by="cli")  # type: ignore[union-attr]
    harness.store.save(run)
    resumed = await harness.run("t", resume=result.run_id, options=RunOptions(interactive=False))
    assert resumed.status is RunStatus.SUCCEEDED
    final = harness.record(result.run_id)
    assert final.pid is None  # cleared on a final status as before …
    assert final.pid_started_at == "Thu Aug 20 12:00:00 2026"  # … the start time was refreshed


def test_run_record_reads_without_the_field() -> None:
    """Older run.json files (no ``pid_started_at``) load with ``None``."""
    run = RunRecord.model_validate(
        {
            "schema": 1,
            "run_id": "20260820-100000-old1",
            "workflow_name": "w",
            "workflow_path": "w.yaml",
            "workflow_hash": "a" * 64,
            "project_slug": "local/x",
            "project_root": "/x",
            "pid": 4242,
        }
    )
    assert run.pid == 4242 and run.pid_started_at is None
