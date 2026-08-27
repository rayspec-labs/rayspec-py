# SPDX-License-Identifier: Apache-2.0
"""B8 (PRD-09 F5/F6): `rayspec plan` shows agent budgets/max_turns and step descriptions — the
pre-flight view of what a run will cost and do, before it spends a token. Input-backed numbers
(E1) show the resolved value when the plan's inputs supply it, else the reference."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

WF = """
rayspec: 1
name: budgeted
inputs:
  cap: {type: number, default: 12}
agents:
  writer:
    provider: stub
    budget_usd: "{{ inputs.cap }}"
    max_turns: 7
steps:
  - id: draft
    description: "Write the first draft from the brief"
    agent: writer
    prompt: go
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "budgeted.yaml").write_text(WF, encoding="utf-8")
    return root


def _plan_json(project: Path, *args: str) -> dict:
    result = CliRunner().invoke(app, ["plan", "budgeted", "--root", str(project), "--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_json_agents_carry_budget_max_turns_and_input_refs(project: Path) -> None:
    data = _plan_json(project)
    agent = next(a for a in data["agents"] if a["name"] == "writer")
    # default input supplies the budget → resolved to 12
    assert agent["budget_usd"] == 12
    assert agent["max_turns"] == 7
    assert agent["input_refs"] == {"budget_usd": "cap"}


def test_a_passed_input_resolves_the_budget_in_the_plan(project: Path) -> None:
    data = _plan_json(project, "--input", "cap=25")
    agent = next(a for a in data["agents"] if a["name"] == "writer")
    assert agent["budget_usd"] == 25


def test_json_steps_carry_the_description(project: Path) -> None:
    data = _plan_json(project)
    draft = next(s for s in data["steps"] if s["path"] == "draft")
    assert draft["description"] == "Write the first draft from the brief"


def test_text_plan_shows_the_budget_and_description(project: Path) -> None:
    result = CliRunner().invoke(app, ["plan", "budgeted", "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert "budget" in result.output and "turns" in result.output
    # the reference must be RESOLVED in the agents table, not shown verbatim: pass a distinctive
    # value so it cannot coincide with the input row's default (12) or anything else on the page
    result = CliRunner().invoke(
        app, ["plan", "budgeted", "--root", str(project), "--input", "cap=37"]
    )
    assert result.exit_code == 0, result.output
    assert "37" in result.output  # the resolved budget in the writer row
    assert "{{ inputs.cap }}" not in result.output  # the reference did not survive into the plan
    assert "Write the first draft" in result.output
