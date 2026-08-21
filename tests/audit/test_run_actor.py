"""``run.json`` carries who launched the run, and every decision carries who decided."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.store.file import FileRunStore

from .conftest import only_store


@pytest.fixture
def paused(
    cli: CliRunner, project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, FileRunStore]:
    monkeypatch.setenv("RAYSPEC_ACTOR", "launcher@example.invalid")
    result = cli.invoke(app, ["run", "gate", "--root", str(project), "--no-interactive"])
    assert result.exit_code == 3, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    return run_id, store


def test_run_json_records_the_launching_actor(paused: tuple[str, FileRunStore]) -> None:
    run_id, store = paused
    run = store.load(run_id)
    assert run.actor is not None
    assert run.actor.id == "launcher@example.invalid"
    assert run.actor.source == "env"


def test_a_decision_records_who_made_it_and_resume_keeps_the_launcher(
    cli: CliRunner,
    project: Path,
    paused: tuple[str, FileRunStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, store = paused
    monkeypatch.setenv("RAYSPEC_ACTOR", "reviewer@example.invalid")
    result = cli.invoke(app, ["approve", run_id, "ship it", "--root", str(project)])
    assert result.exit_code == 0, result.output
    run = store.load(run_id)
    # the run keeps naming whoever started it, even though somebody else resumed it
    assert run.actor is not None and run.actor.id == "launcher@example.invalid"
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    assert decisions, "the resumed gate must emit a decision event"
    actor = decisions[-1].data.get("actor")
    assert actor is not None and actor["id"] == "reviewer@example.invalid"
    assert decisions[-1].data["by"] == "cli"


def test_a_decision_is_stamped_before_the_gate_consumes_it(
    cli: CliRunner, project: Path, paused: tuple[str, FileRunStore], monkeypatch
) -> None:
    from rayspec.cli.commands.approve import record_decision

    run_id, store = paused
    monkeypatch.setenv("RAYSPEC_ACTOR", "reviewer@example.invalid")
    run = store.load(run_id)
    decision = record_decision(store, run, approved=True, comment="ok")
    assert decision.actor is not None and decision.actor.id == "reviewer@example.invalid"
    reloaded = store.load(run_id)
    assert reloaded.pause is not None and reloaded.pause.decision is not None
    assert reloaded.pause.decision.actor is not None
    assert reloaded.pause.decision.actor.id == "reviewer@example.invalid"


def test_a_terminal_approval_is_attributed_to_the_run_actor(
    cli: CliRunner, project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAYSPEC_ACTOR", "launcher@example.invalid")
    result = cli.invoke(app, ["run", "gate", "--root", str(project), "--yes"])
    assert result.exit_code == 0, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    decisions = [e for e in store.read_events(run_id) if e.type.value == "run.decision"]
    assert decisions and decisions[-1].data["by"] == "--yes"
    actor = decisions[-1].data.get("actor")
    assert actor is not None and actor["id"] == "launcher@example.invalid"
