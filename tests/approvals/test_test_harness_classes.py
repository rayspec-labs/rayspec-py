"""`rayspec test` and approval classes.

`--exec-shell` is the mode where a case's shell bodies really run, in the project directory —
so it is exactly the unattended context an operator's `allow_yes: false` exists to protect, and
the case file that would authorise it sits in the repository next to the workflow.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.engine.approval_classes import ClassRules

from .conftest import Tree

runner = CliRunner()

SHIP = """
rayspec: 1
name: ship
isolation: none
steps:
  - id: gate
    approve:
      message: publish?
      class: release
  - id: publish
    needs: [gate]
    shell: echo published > published_by_test.txt
"""

CASE = """
id: happy
workflow: ship
exec_shell: true
expect:
  status: succeeded
"""


@pytest.fixture
def suite(tree: Tree) -> Path:
    tree.workflow("ship", SHIP)
    case = tree.root / ".rayspec" / "tests" / "ship" / "happy.yaml"
    case.parent.mkdir(parents=True, exist_ok=True)
    case.write_text(textwrap.dedent(CASE).lstrip("\n"), encoding="utf-8")
    return tree.root


def test_a_locked_class_holds_a_case_too(suite: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A case cannot approve what the operator's policy holds shut, even with --exec-shell."""
    monkeypatch.setattr(
        "rayspec.cli.commands.run.policy_class_rules",
        lambda project_root, home: {"release": ClassRules(allow_yes=False)},
    )
    res = runner.invoke(app, ["test", "--root", str(suite), "--exec-shell"])
    assert res.exit_code == 1, res.output
    assert not (suite / "published_by_test.txt").exists(), "the gated step ran anyway"


def test_a_case_still_passes_when_no_policy_holds_the_class(suite: Path) -> None:
    """Without a policy the harness behaves exactly as it did: a gate a case reaches is
    approved by the dry run, so an ordinary suite is unaffected."""
    res = runner.invoke(app, ["test", "--root", str(suite), "--exec-shell"])
    assert res.exit_code == 0, res.output
    assert (suite / "published_by_test.txt").exists()


def test_the_harness_does_not_read_a_policy_itself() -> None:
    """Whoever drives a case supplies the rules, so `testing/` keeps depending on nothing
    above it — the CLI is what knows where an operator's rules come from."""
    import inspect

    from rayspec.testing import runner

    parameter = inspect.signature(runner.run_case).parameters["approval_classes"]
    assert parameter.default is None  # no rules unless the caller has some
    assert "rayspec.cli" not in Path(inspect.getsourcefile(runner) or "").read_text()
