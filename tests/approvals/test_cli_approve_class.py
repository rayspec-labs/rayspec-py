"""`--approve-class` on `rayspec run` / `rayspec resume`, and the policy seam behind it."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from rayspec.cli.app import app
from rayspec.engine.approval_classes import ClassRules

from .conftest import Tree

runner = CliRunner()

TWO_GATES = """
rayspec: 1
name: ship
isolation: none
steps:
  - id: build
    shell: echo built
  - id: chore
    needs: [build]
    approve:
      message: tidy up?
      class: chore
  - id: release
    needs: [build]
    approve:
      message: publish?
      class: release
"""


@pytest.fixture
def project(tree: Tree) -> Path:
    tree.workflow("ship", TWO_GATES)
    return tree.root


def run(project: Path, *args: str) -> Result:
    return runner.invoke(app, ["run", "ship", "--root", str(project), "--no-interactive", *args])


def test_a_pre_authorised_class_is_approved_and_the_others_still_ask(project: Path) -> None:
    res = run(project, "--approve-class", "release")
    assert res.exit_code == 3, res.output
    assert "publish?" not in res.output  # the release gate never asked
    assert "tidy up?" in res.output


def test_without_the_flag_both_gates_ask(project: Path) -> None:
    res = run(project)
    assert res.exit_code == 3, res.output


def test_the_option_is_repeatable(project: Path) -> None:
    res = run(project, "--approve-class", "release", "--approve-class", "chore")
    assert res.exit_code == 0, res.output


def test_a_class_no_gate_uses_pre_authorises_nothing(project: Path) -> None:
    res = run(project, "--approve-class", "relase")
    assert res.exit_code == 3, res.output


def test_policy_rules_reach_the_run_and_yes_cannot_waive_them(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point, through the CLI: an operator's policy holds against every flag."""
    monkeypatch.setattr(
        "rayspec.cli.commands.run.policy_class_rules",
        lambda project_root, home: {"release": ClassRules(allow_yes=False)},
    )
    res = run(project, "--yes", "--approve-class", "release", "--dry-run")
    assert res.exit_code == 3, res.output
    assert "--yes does not approve approval class 'release'" in res.output


def test_resume_takes_the_flag_too(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = run(project)
    assert first.exit_code == 3, first.output
    run_id = next(
        line.split()[-1] for line in first.output.splitlines() if "rayspec resume" in line
    )
    res = runner.invoke(
        app,
        [
            "resume",
            run_id,
            "--root",
            str(project),
            "--no-interactive",
            "--approve-class",
            "chore",
            "--approve-class",
            "release",
        ],
    )
    assert res.exit_code == 0, res.output


LOCKED_GATE = """
rayspec: 1
name: locked
isolation: none
steps:
  - id: build
    shell: echo built
  - id: gate
    needs: [build]
    approve:
      message: publish?
      class: release
  - id: publish
    needs: [gate]
    shell: echo published
"""


def test_rayspec_approve_cannot_answer_a_require_tty_gate(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rayspec approve` can be scripted from cron, so `require_tty` does not accept it —
    and says so, pointing at the command that does."""
    tree.workflow("locked", LOCKED_GATE)
    monkeypatch.setattr(
        "rayspec.cli.commands.run.policy_class_rules",
        lambda project_root, home: {"release": ClassRules(allow_yes=False, require_tty=True)},
    )
    first = runner.invoke(app, ["run", "locked", "--root", str(tree.root), "--no-interactive"])
    assert first.exit_code == 3, first.output
    run_id = next(
        line.split()[-1] for line in first.output.splitlines() if "rayspec resume" in line
    )
    res = runner.invoke(app, ["approve", run_id, "ship it", "--root", str(tree.root)])
    assert res.exit_code == 3, res.output
    assert "requires a terminal" in res.output
    assert "rayspec resume" in res.output
