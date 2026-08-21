# SPDX-License-Identifier: Apache-2.0
"""`rayspec explain` on a step the run's teardown skipped, not its own ``needs``.

The join row re-evaluates the truth table so a reader can see the verdict rather than take the
recorded status on trust. That re-evaluation has to be told what the scheduler knew: a step
skipped ``run_failed`` was decided while the list was draining, and the same table read as if the
run were healthy says the step should have run — two lines under the recorded skip reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.store.file import FileRunStore

DRAIN_WF = """
rayspec: 1
name: t
isolation: none
defaults:
  on_step_failure: fail_fast
steps:
  - {id: warmup, shell: "sleep 0.05"}
  - {id: bad, needs: [warmup], shell: "exit 7"}
  - {id: slow, shell: "sleep 5"}
  - {id: cleanup, needs: [bad], join: always, shell: "true"}
  - {id: post, needs: [cleanup], shell: "true"}
"""


@pytest.fixture
def drained(cli: CliRunner, home: Path, project: Path) -> tuple[str, Path]:
    """A fail-fast run whose ``cleanup`` ran and whose ``post`` was skipped by the teardown."""
    (project / ".rayspec" / "workflows" / "t.yaml").write_text(DRAIN_WF, encoding="utf-8")
    result = cli.invoke(app, ["run", "t", "--root", str(project), "--quiet"])
    assert result.exit_code == 1, result.output
    store = FileRunStore(home / "projects" / project_slug_for(project))
    run_id = store.list_run_ids()[0]
    steps = store.load(run_id).steps
    assert steps["cleanup"].status.value == "succeeded"
    assert steps["post"].skip_reason == "run_failed"
    return run_id, project


def test_the_join_row_does_not_contradict_the_recorded_skip(
    cli: CliRunner, drained: tuple[str, Path]
) -> None:
    """``post`` was skipped because the run was already failing, and the row has to say so."""
    run_id, project = drained
    result = cli.invoke(app, ["explain", run_id, "post", "--root", str(project), "--json"])
    assert result.exit_code == 0, result.output
    join = json.loads(result.stdout)["join"]
    assert join["draining"] is True
    assert join["decision"] == "skip"
    assert join["skip_reason"] == "run_failed"


def test_the_text_view_says_the_list_was_draining(
    cli: CliRunner, drained: tuple[str, Path]
) -> None:
    run_id, project = drained
    result = cli.invoke(app, ["explain", run_id, "post", "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert "decision run" not in result.stdout
    assert "the run was already draining" in result.stdout


def test_a_healthy_step_keeps_a_plain_join_row(cli: CliRunner, drained: tuple[str, Path]) -> None:
    """Nothing changes for a step the run did not tear down."""
    run_id, project = drained
    result = cli.invoke(app, ["explain", run_id, "cleanup", "--root", str(project), "--json"])
    assert result.exit_code == 0, result.output
    join = json.loads(result.stdout)["join"]
    assert join["draining"] is False and join["decision"] == "run"
