"""`rayspec approve|reject|resume` on real runs (stub provider) through the engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.store.file import FileRunStore

from .conftest import PAUSED_ID, SUCCEEDED_ID, Seeded

GATE = """
rayspec: 1
name: gate
isolation: none
steps:
  - {id: a, shell: echo a}
  - id: plan
    needs: [a]
    agent: {provider: stub}
    prompt: "plan for {{ steps.a.output }}"
  - {id: ok, needs: [plan], approve: "ship {{ steps.plan.output }}?"}
  - {id: b, needs: [ok], shell: "echo {{ steps.ok.output }}"}
outputs:
  plan: "{{ steps.plan.output }}"
  note: "{{ steps.b.output }}"
"""

CRASH = """
rayspec: 1
name: crash
isolation: none
steps:
  - {id: a, shell: echo a}
  - {id: b, needs: [a], shell: cat flag.txt}
"""


@pytest.fixture
def gate_project(home: Path, project: Path) -> Path:
    (project / ".rayspec" / "workflows" / "gate.yaml").write_text(GATE, encoding="utf-8")
    (project / ".rayspec" / "workflows" / "crash.yaml").write_text(CRASH, encoding="utf-8")
    return project


def _store(home: Path) -> FileRunStore:
    (slug_dir,) = [p for p in (home / "projects").glob("*/*") if (p / "runs").is_dir()]
    return FileRunStore(slug_dir)


@pytest.fixture
def paused(cli: CliRunner, gate_project: Path, home: Path) -> tuple[str, FileRunStore]:
    result = cli.invoke(app, ["run", "gate", "--root", str(gate_project), "--no-interactive"])
    assert result.exit_code == 3, result.output
    store = _store(home)
    (run_id,) = store.list_run_ids()
    run = store.load(run_id)
    assert run.status.value == "paused" and run.pause is not None
    return run_id, store


def test_approve_resumes_through_engine_and_completes(
    cli: CliRunner, gate_project: Path, paused: tuple[str, FileRunStore]
) -> None:
    run_id, store = paused
    result = cli.invoke(app, ["approve", run_id[:14], "ship it", "--root", str(gate_project)])
    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output
    run = store.load(run_id)
    assert run.status.value == "succeeded"
    assert run.steps["ok"].status.value == "succeeded" and run.steps["ok"].approved is True
    assert store.read_output(run_id, run.steps["b"].output_ref or "") == "ship it"
    assert run.outputs == {"plan": "[stub] plan for a", "note": "ship it"}
    assert run.steps["plan"].provider == "stub" and run.steps["plan"].attempts == 1
    assert run.pause is None
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    assert decisions and decisions[-1].data["by"] == "cli" and decisions[-1].data["approved"]
    assert decisions[-1].data["comment"] == "ship it"


def test_reject_cancels_the_run_exit_4(
    cli: CliRunner, gate_project: Path, paused: tuple[str, FileRunStore]
) -> None:
    run_id, store = paused
    result = cli.invoke(app, ["reject", run_id, "not now", "--root", str(gate_project)])
    assert result.exit_code == 4, result.output
    assert "cancelled" in result.output
    run = store.load(run_id)
    assert run.status.value == "cancelled"
    assert run.steps["ok"].status.value == "rejected"
    assert run.steps["b"].status.value == "skipped"
    assert run.reason and "not now" in run.reason


def test_approve_json_emits_events_and_summary(
    cli: CliRunner, gate_project: Path, paused: tuple[str, FileRunStore]
) -> None:
    run_id, _ = paused
    result = cli.invoke(app, ["approve", run_id, "--json", "--root", str(gate_project)])
    assert result.exit_code == 0, result.output
    # JSONL events AND the final summary object go to stdout (CONTRACTS: machine consumers pipe
    # stdout); nothing non-JSON is printed there
    stdout_lines = [line for line in result.stdout.splitlines() if line.strip()]
    lines = [json.loads(line) for line in stdout_lines]
    types = [line.get("type") for line in lines]
    assert types[0] == "run.resumed" and "run.decision" in types
    (summary,) = [line for line in lines if "exit_code" in line]  # summary (mirrors `run --json`)
    assert summary["run_id"] == run_id and summary["status"] == "succeeded"
    assert summary["exit_code"] == 0 and summary["outputs"]["note"] == ""


def test_approve_requires_paused_status(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["approve", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 2
    assert "not paused" in result.output and "succeeded" in result.output
    unknown = cli.invoke(app, ["reject", "zzz", "--root", str(seeded.project)])
    assert unknown.exit_code == 2 and "no run matches" in unknown.output


def test_approve_seeded_paused_run_without_workflow_is_a_usage_error(
    cli: CliRunner, seeded: Seeded
) -> None:
    # the decision is recorded only when the workflow can be re-loaded
    result = cli.invoke(app, ["approve", PAUSED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 2, result.output
    run = seeded.store.load(PAUSED_ID)
    assert run.pause is not None and run.pause.decision is None


def test_resume_paused_non_tty_points_to_approve(
    cli: CliRunner, gate_project: Path, paused: tuple[str, FileRunStore]
) -> None:
    run_id, store = paused
    result = cli.invoke(app, ["resume", run_id, "--root", str(gate_project)])
    assert result.exit_code == 3, result.output
    assert f"rayspec approve {run_id}" in result.output
    assert store.load(run_id).status.value == "paused"
    # --yes auto-approves the pending gate
    auto = cli.invoke(app, ["resume", run_id, "--yes", "--root", str(gate_project)])
    assert auto.exit_code == 0, auto.output
    run = store.load(run_id)
    assert run.status.value == "succeeded" and run.resume_count == 1
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    assert decisions[-1].data["by"] == "--yes"


def test_resume_failed_run_reuses_steps(cli: CliRunner, gate_project: Path, home: Path) -> None:
    failed = cli.invoke(app, ["run", "crash", "--root", str(gate_project)])
    assert failed.exit_code == 1, failed.output
    store = _store(home)
    (run_id,) = store.list_run_ids()
    (gate_project / "flag.txt").write_text("flag\n", encoding="utf-8")
    result = cli.invoke(app, ["resume", run_id[:12], "--root", str(gate_project)])
    assert result.exit_code == 0, result.output
    assert "reused 1 step" in result.output
    run = store.load(run_id)
    assert run.status.value == "succeeded" and run.steps["b"].attempts == 2
    # a workflow change refuses without --force
    (gate_project / ".rayspec" / "workflows" / "crash.yaml").write_text(
        CRASH + "  - {id: c, needs: [b], shell: echo c}\n", encoding="utf-8"
    )
    # (a succeeded run is refused before the hash check; --force bypasses both)
    refused = cli.invoke(app, ["resume", run_id, "--root", str(gate_project)])
    assert refused.exit_code == 2 and "already succeeded" in refused.output
    forced = cli.invoke(app, ["resume", run_id, "--force", "--json", "--root", str(gate_project)])
    assert forced.exit_code == 0, forced.output
    assert store.load(run_id).steps["c"].status.value == "succeeded"


def test_resume_unknown_and_quiet(cli: CliRunner, gate_project: Path) -> None:
    result = cli.invoke(app, ["resume", "nope", "--root", str(gate_project)])
    assert result.exit_code == 2 and "no run matches" in result.output


def test_resume_paused_hint_is_safe_against_markup_in_pause_message(
    cli: CliRunner, seeded: Seeded
) -> None:
    run = seeded.store.load(PAUSED_ID)
    assert run.pause is not None
    run.pause.message = "ship [x] to [/bold] prod?"
    # resume re-loads the workflow first — give the seeded run a real, unchanged one
    from rayspec.config import Config
    from rayspec.loader import load_workflow

    (seeded.project / ".rayspec" / "workflows" / "gate.yaml").write_text(GATE, encoding="utf-8")
    run.workflow_hash = load_workflow(
        "gate", project_root=seeded.project, home=seeded.home, config=Config()
    ).hash
    seeded.store.save(run)
    result = cli.invoke(app, ["resume", PAUSED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 3, result.output
    assert "ship [x] to [/bold] prod?" in result.output


def test_approve_after_workflow_change_records_nothing(
    cli: CliRunner, gate_project: Path, paused: tuple[str, FileRunStore]
) -> None:
    run_id, store = paused
    (gate_project / ".rayspec" / "workflows" / "gate.yaml").write_text(
        GATE + '  extra: "{{ steps.a.output }}"\n', encoding="utf-8"
    )
    result = cli.invoke(app, ["approve", run_id, "ok", "--root", str(gate_project)])
    assert result.exit_code == 2, result.output
    assert "changed" in result.output and "--force" in result.output
    run = store.load(run_id)
    # no half-applied state: the decision must not survive a refused resume
    assert run.status.value == "paused" and run.pause is not None
    assert run.pause.decision is None and run.resume_count == 0
    # a later plain resume still asks (non-TTY ⇒ exit 3 hint), it does not auto-consume anything
    later = cli.invoke(app, ["resume", run_id, "--force", "--root", str(gate_project)])
    assert later.exit_code == 3, later.output
    assert store.load(run_id).steps["ok"].approved is None
    # --force approves despite the change
    forced = cli.invoke(app, ["approve", run_id, "ok", "--force", "--root", str(gate_project)])
    assert forced.exit_code == 0, forced.output
    assert store.load(run_id).status.value == "succeeded"
