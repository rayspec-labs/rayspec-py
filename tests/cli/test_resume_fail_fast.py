"""``rayspec run --fail-fast`` is recorded, and ``rayspec resume --fail-fast`` can supply it.

The engine owns the rule (``RunRecord.fail_fast`` is restored on every resume entry and the flag
may only tighten it — ``tests/engine/test_fail_fast_resume.py``). What this pins is the wiring:
the flag exists on ``resume``, it reaches ``RunOptions``, and the launch flag actually lands in
``run.json`` — without which every resume entry, ``approve`` and ``reject`` included, would keep
running the second half of a run under a looser failure policy than the first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.store.file import FileRunStore

GATED = """
rayspec: 1
name: gate
isolation: none
steps:
  - {id: gate, approve: "go?"}
  - {id: boom, needs: [gate], shell: "exit 7"}
  - {id: slow, needs: [gate], shell: "sleep 5"}
"""


@pytest.fixture
def paused(cli: CliRunner, home: Path, project: Path) -> tuple[str, FileRunStore]:
    (project / ".rayspec" / "workflows" / "gate.yaml").write_text(GATED, encoding="utf-8")
    result = cli.invoke(app, ["run", "gate", "--root", str(project), "--no-interactive"])
    assert result.exit_code == 3, result.output
    (slug_dir,) = [p for p in (home / "projects").glob("*/*") if (p / "runs").is_dir()]
    store = FileRunStore(slug_dir)
    (run_id,) = store.list_run_ids()
    return run_id, store


def test_the_launch_flag_is_recorded(cli: CliRunner, home: Path, project: Path) -> None:
    (project / ".rayspec" / "workflows" / "gate.yaml").write_text(GATED, encoding="utf-8")
    result = cli.invoke(
        app, ["run", "gate", "--root", str(project), "--no-interactive", "--fail-fast"]
    )
    assert result.exit_code == 3, result.output
    (slug_dir,) = [p for p in (home / "projects").glob("*/*") if (p / "runs").is_dir()]
    store = FileRunStore(slug_dir)
    (run_id,) = store.list_run_ids()
    assert store.load(run_id).fail_fast is True


def test_resume_accepts_fail_fast_and_it_takes_effect(
    cli: CliRunner, paused: tuple[str, FileRunStore]
) -> None:
    run_id, store = paused
    result = cli.invoke(app, ["resume", run_id, "--yes", "--fail-fast"])
    assert result.exit_code == 1, result.output
    run = store.load(run_id)
    assert run.fail_fast is True, "the flag given on the resume is recorded in turn"
    assert run.steps["slow"].status.value == "interrupted"


def test_a_plain_resume_does_not_turn_it_on(
    cli: CliRunner, paused: tuple[str, FileRunStore]
) -> None:
    """The control: without the flag (and without a recorded one) the run still drains."""
    run_id, store = paused
    result = cli.invoke(app, ["resume", run_id, "--yes"])
    assert result.exit_code == 1, result.output
    run = store.load(run_id)
    assert run.fail_fast is False
    assert run.steps["slow"].status.value == "succeeded"
