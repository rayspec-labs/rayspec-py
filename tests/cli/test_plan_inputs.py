"""`rayspec plan` input diagnostics: one problem per input."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

runner = CliRunner()

KITCHEN = """\
rayspec: 1
name: kitchen
inputs:
  issue: { type: integer, required: true }
  target: { type: string, default: "." }
steps:
  - id: a
    shell: echo hi
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    monkeypatch.delenv("RAYSPEC_INPUT_ISSUE", raising=False)
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "kitchen.yaml").write_text(KITCHEN, encoding="utf-8")
    return root


def test_plan_invalid_required_input_shows_one_problem(project: Path) -> None:
    res = runner.invoke(
        app,
        ["plan", "kitchen", "--input", "issue=abc", "--input", "nope=1", "--root", str(project)],
    )
    assert res.exit_code == 2, res.output
    assert "issue = 'abc' (invalid: expected an integer, got 'abc')" in res.output
    assert "missing (required)" not in res.output
    assert "missing required input(s)" not in res.output
    assert "error: input 'issue': expected an integer, got 'abc'" in res.output
    assert "error: --input: unknown input 'nope' (declared: issue, target)" in res.output
    assert res.output.count("error:") == 2


def test_plan_json_invalid_required_input_state_is_invalid_only(project: Path) -> None:
    res = runner.invoke(
        app, ["plan", "kitchen", "--input", "issue=abc", "--json", "--root", str(project)]
    )
    assert res.exit_code == 2
    data = json.loads(res.output)
    row = data["inputs"]["issue"]
    assert row["state"] == "invalid"
    assert row["problem"] == "expected an integer, got 'abc'"
    assert data["input_errors"] == ["input 'issue': expected an integer, got 'abc'"]


def test_plan_missing_required_input_is_still_missing(project: Path) -> None:
    res = runner.invoke(app, ["plan", "kitchen", "--root", str(project)])
    assert res.exit_code == 2
    assert "issue = missing (required)" in res.output
    assert "missing required input(s): issue" in res.output


def test_bracketed_input_value_survives_rich_markup(project: Path) -> None:
    """User text in the error path (input values, step paths) must never be read as markup —
    the same bug class shows up on the plan/run error path."""
    res = runner.invoke(
        app, ["plan", "kitchen", "--input", "issue=[red]x[/red]", "--root", str(project)]
    )
    assert res.exit_code == 2, res.output
    assert "error: input 'issue': expected an integer, got '[red]x[/red]'" in res.output
    assert "issue = '[red]x[/red]' (invalid: expected an integer, got '[red]x[/red]')" in res.output


def test_bracketed_string_input_value_is_shown_verbatim(project: Path) -> None:
    res = runner.invoke(
        app,
        [
            "plan",
            "kitchen",
            "--input",
            "issue=3",
            "--input",
            "target=[b]x[/b]",
            "--root",
            str(project),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "target = [b]x[/b]" in res.output
