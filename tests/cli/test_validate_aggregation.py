# SPDX-License-Identifier: Apache-2.0
"""`rayspec validate` reports every schema mistake of a document, each with its file:line."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

runner = CliRunner()

THREE_MISTAKES = """\
rayspec: 1
name: broken
descriptionn: oops
steps:
  - id: one
    shell: echo hi
    timeoutt: 5m
  - id: Two
    shell: echo bye
"""

BAD_AGENT = """\
provider: claude
modell: small
"""

AGENT_WORKFLOW = """\
rayspec: 1
name: uses_agent
steps:
  - id: review
    agent: reviewer
    prompt: hi
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "agents").mkdir(parents=True)
    return root


def _write(project: Path, name: str, text: str) -> None:
    (project / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text, encoding="utf-8")


def test_three_mistakes_are_all_reported_with_file_and_line(project: Path) -> None:
    _write(project, "broken", THREE_MISTAKES)
    res = runner.invoke(app, ["validate", "broken", "--root", str(project)])
    assert res.exit_code == 2, res.output
    bullets = [ln for ln in res.output.splitlines() if ln.startswith("  - ")]
    assert len(bullets) == 3, res.output
    assert ".rayspec/workflows/broken.yaml:3:" in bullets[0]
    assert "descriptionn" in bullets[0]
    assert ".rayspec/workflows/broken.yaml:7:" in bullets[1]
    assert "timeoutt" in bullets[1]
    assert ".rayspec/workflows/broken.yaml:8:" in bullets[2]
    assert "'Two'" in bullets[2]
    assert "3 error(s)" in res.output


def test_json_has_one_object_per_problem_with_a_non_null_path(project: Path) -> None:
    _write(project, "broken", THREE_MISTAKES)
    res = runner.invoke(app, ["validate", "broken", "--json", "--root", str(project)])
    assert res.exit_code == 2
    [row] = json.loads(res.output)
    problems = row["problems"]
    assert len(problems) == 3, problems
    assert all(p["path"] for p in problems), problems
    assert [p["line"] for p in problems] == [3, 7, 8]
    assert [p["location"] for p in problems] == [
        ".rayspec/workflows/broken.yaml:3",
        ".rayspec/workflows/broken.yaml:7",
        ".rayspec/workflows/broken.yaml:8",
    ]
    assert problems[0]["hint"] == "did you mean 'description'?"
    assert row["errors"] == [p["message"] for p in problems] or len(row["errors"]) == 3


def test_every_problem_of_a_valid_workflow_row_still_has_a_path(project: Path) -> None:
    _write(project, "ok", "rayspec: 1\nname: ok\nsteps:\n  - id: a\n    shell: echo hi\n")
    res = runner.invoke(app, ["validate", "ok", "--json", "--root", str(project)])
    assert res.exit_code == 0
    [row] = json.loads(res.output)
    assert row["ok"] is True
    assert row["problems"] == []


def test_a_yaml_syntax_error_is_one_problem_with_a_path(project: Path) -> None:
    _write(project, "syntax", "a: [\n")
    res = runner.invoke(app, ["validate", "syntax", "--json", "--root", str(project)])
    assert res.exit_code == 2
    [row] = json.loads(res.output)
    assert len(row["problems"]) == 1
    assert row["problems"][0]["path"] == ".rayspec/workflows/syntax.yaml"


def test_agent_file_problems_carry_their_own_file_and_line(project: Path) -> None:
    _write(project, "uses_agent", AGENT_WORKFLOW)
    (project / ".rayspec" / "agents" / "reviewer.yaml").write_text(BAD_AGENT, encoding="utf-8")
    res = runner.invoke(app, ["validate", "uses_agent", "--json", "--root", str(project)])
    assert res.exit_code == 2
    [row] = json.loads(res.output)
    [problem] = row["problems"]
    assert problem["path"] == ".rayspec/agents/reviewer.yaml"
    assert problem["line"] == 2
    assert "modell" in problem["message"]


def test_validation_report_errors_are_problems_too(project: Path) -> None:
    """A graph/reference error (not a schema error) is still one JSON problem with a path."""
    _write(
        project,
        "dangling",
        "rayspec: 1\nname: dangling\nsteps:\n  - id: a\n    needs: [nope]\n    shell: echo hi\n",
    )
    res = runner.invoke(app, ["validate", "dangling", "--json", "--root", str(project)])
    assert res.exit_code == 2
    [row] = json.loads(res.output)
    assert row["problems"], row
    assert all(p["path"] == ".rayspec/workflows/dangling.yaml" for p in row["problems"])


INCLUDING = """\
rayspec: 1
name: outer
steps:
  - id: first
    shell: echo hi
  - id: body
    include: inner
"""

INCLUDED = """\
rayspec: 1
name: inner
steps:
  - id: a
    shell: echo a
  - id: b
    needs: [nope]
    shell: echo b
"""


def test_a_problem_in_an_included_document_names_that_document(project: Path) -> None:
    """Review: `path` + `line` are advertised as jump targets — they must not name the includer."""
    _write(project, "outer", INCLUDING)
    _write(project, "inner", INCLUDED)
    res = runner.invoke(app, ["validate", "outer", "--root", str(project), "--json"])
    assert res.exit_code == 2, res.output
    [row] = json.loads(res.output)
    problems = row["problems"]
    assert len(problems) == 1, problems
    assert problems[0]["path"] == ".rayspec/workflows/inner.yaml"
    assert problems[0]["line"] == 7  # the `needs:` line of inner.yaml
    assert problems[0]["location"] == ".rayspec/workflows/inner.yaml:7"


def test_a_problem_without_a_location_still_names_the_workflow(project: Path) -> None:
    _write(project, "outer", INCLUDING)
    _write(project, "inner", INCLUDED.replace("needs: [nope]", "needs: [a]\n    join: nope"))
    res = runner.invoke(app, ["validate", "outer", "--root", str(project), "--json"])
    assert res.exit_code == 2, res.output
    [row] = json.loads(res.output)
    problems = row["problems"]
    assert all(p["path"] for p in problems), problems
