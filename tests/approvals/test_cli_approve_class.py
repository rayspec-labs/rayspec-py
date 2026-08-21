"""`--approve-class` on `rayspec run` / `rayspec resume`, and the policy seam behind it."""

from __future__ import annotations

import re
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


def run_id_of(res: Result) -> str:
    """The run id out of a command's output (every hint quotes it, so match its shape)."""
    match = re.search(r"\b\d{8}-\d{6}-[a-z0-9]{4}\b", res.output)
    assert match is not None, res.output
    return match.group(0)


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
    run_id = run_id_of(first)
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
    run_id = run_id_of(first)
    res = runner.invoke(app, ["approve", run_id, "ship it", "--root", str(tree.root)])
    assert res.exit_code == 3, res.output
    assert "requires a terminal" in res.output
    assert "rayspec resume" in res.output


def test_a_named_class_no_policy_holds_is_reported_when_the_run_reaches_it(project: Path) -> None:
    """The half of the feature that RESTRICTS is not wired up yet, so a workflow that names a
    class must not read as if it were locked."""
    res = run(project, "--approve-class", "chore")
    assert "no operator policy is in force" in res.output, res.output
    assert "release" in res.output


def test_a_require_tty_pause_does_not_advertise_rayspec_approve(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pause hint used to recommend the one command this class refuses."""
    tree.workflow("locked", LOCKED_GATE)
    monkeypatch.setattr(
        "rayspec.cli.commands.run.policy_class_rules",
        lambda project_root, home: {"release": ClassRules(allow_yes=False, require_tty=True)},
    )
    res = runner.invoke(app, ["run", "locked", "--root", str(tree.root), "--no-interactive"])
    assert res.exit_code == 3, res.output
    decide = [line for line in res.output.splitlines() if "decide with" in line]
    assert decide, res.output
    assert "rayspec approve" not in decide[0]
    assert "rayspec resume" in decide[0]


def test_an_ordinary_pause_still_offers_approve_and_reject(project: Path) -> None:
    res = run(project)
    decide = [line for line in res.output.splitlines() if "decide with" in line]
    assert decide, res.output
    assert "rayspec approve" in decide[0]
    assert "rayspec reject" in decide[0]


def test_the_still_paused_pointer_is_class_aware(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rayspec resume` short-circuits a paused run with a pointer; under a class that needs a
    terminal, `rayspec approve` and `--yes` are not what it should point at."""
    tree.workflow("locked", LOCKED_GATE)
    monkeypatch.setattr(
        "rayspec.cli.commands.run.policy_class_rules",
        lambda project_root, home: {"release": ClassRules(allow_yes=False, require_tty=True)},
    )
    first = runner.invoke(app, ["run", "locked", "--root", str(tree.root), "--no-interactive"])
    assert first.exit_code == 3, first.output
    res = runner.invoke(
        app, ["resume", run_id_of(first), "--root", str(tree.root), "--no-interactive"]
    )
    assert res.exit_code == 3, res.output
    assert "rayspec approve" not in res.output
    assert "--yes" not in res.output
    assert "from a terminal" in res.output


def test_plan_names_a_gate_whose_class_nothing_holds(project: Path) -> None:
    """Before a run: the workflow names two classes and nothing defines either of them."""
    res = runner.invoke(app, ["plan", "ship", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "no operator policy is in force" in res.output
    assert "'release'" in res.output
