"""`rayspec plan` header: the run-level caps the workflow put on the run.

Three `defaults:` keys cap a whole run — `budget_usd`, `max_tokens` and `timeout_total` — and
they are one circuit breaker. Printing two of them is worse than printing none: a reader who
sees a cost cap and a token cap next to each other concludes there is no time cap. Both
renderings (the header line and `--json`) therefore carry all three.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

runner = CliRunner()

CAPPED = """
rayspec: 1
name: wf
defaults:
  budget_usd: 1.5
  max_tokens: 500000
  timeout_total: 2h
steps:
  - {id: a, shell: "echo hi"}
"""

TIME_ONLY = """
rayspec: 1
name: wf
defaults:
  timeout_total: 30m
steps:
  - {id: a, shell: "echo hi"}
"""

UNCAPPED = """
rayspec: 1
name: wf
steps:
  - {id: a, shell: "echo hi"}
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    return root


def _write(root: Path, text: str) -> None:
    (root / ".rayspec" / "workflows" / "wf.yaml").write_text(text, encoding="utf-8")


def _plan(root: Path, *args: str):
    return runner.invoke(app, ["plan", "wf", "--root", str(root), *args])


def _header(output: str) -> str:
    return next(line for line in output.splitlines() if "isolation" in line)


def test_the_header_names_all_three_run_level_caps(project: Path) -> None:
    _write(project, CAPPED)
    result = _plan(project)
    assert result.exit_code == 0, result.output
    header = _header(result.output)
    assert "budget_usd $1.50" in header
    assert "max_tokens 500,000" in header
    assert "timeout_total 2h 0m" in header  # the wording the cap breach itself uses


def test_a_time_cap_alone_is_shown(project: Path) -> None:
    """The bug this pins: a workflow whose only run-level cap is time looked uncapped."""
    _write(project, TIME_ONLY)
    result = _plan(project)
    assert result.exit_code == 0, result.output
    assert "timeout_total 30m 0s" in _header(result.output)


def test_a_workflow_without_caps_says_nothing_about_them(project: Path) -> None:
    _write(project, UNCAPPED)
    result = _plan(project)
    assert result.exit_code == 0, result.output
    header = _header(result.output)
    for cap in ("budget_usd", "max_tokens", "timeout_total"):
        assert cap not in header


def test_json_carries_the_three_caps(project: Path) -> None:
    _write(project, CAPPED)
    result = _plan(project, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["budget_usd"] == 1.5
    assert payload["max_tokens"] == 500000
    assert payload["timeout_total"] == 7200.0  # seconds, like every other duration in the API


def test_json_reports_an_absent_cap_as_null(project: Path) -> None:
    _write(project, UNCAPPED)
    result = _plan(project, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["budget_usd"] is None
    assert payload["max_tokens"] is None
    assert payload["timeout_total"] is None
