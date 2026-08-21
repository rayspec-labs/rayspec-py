"""`rayspec plan` capability report: the pricing nudge for providers that report no cost."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

runner = CliRunner()

WORKFLOW = """
rayspec: 1
name: wf
agents:
  fast: {provider: claude, model: small}
steps:
  - id: a
    agent: {provider: codex, model: large}
    prompt: x
  - id: b
    agent: fast
    prompt: y
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "wf.yaml").write_text(WORKFLOW)
    return root


def _plan(root: Path, *args: str):
    return runner.invoke(app, ["plan", "wf", "--root", str(root), *args])


def test_plan_nudges_when_codex_has_no_pricing(project: Path) -> None:
    res = _plan(project)
    assert res.exit_code == 0, res.output
    assert "provider claude:" in res.output and "reported by the provider" in res.output
    assert "provider codex:" in res.output
    assert "tokens only — add pricing.gpt-5.4 for estimates" in res.output
    assert "docs/providers.md#pricing" in res.output
    assert "estimated from the pricing table" not in res.output


def test_plan_json_reports_cost_source_per_provider(project: Path) -> None:
    res = _plan(project, "--json")
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["providers"]["claude"]["cost"] == "provider"
    assert data["providers"]["claude"]["unpriced_models"] == []
    assert data["providers"]["codex"] == {
        "structured_output": "enforced",
        "cost_reporting": False,
        "cost": "none",
        "priced_models": [],
        "unpriced_models": ["gpt-5.4"],
        "disabled_models": [],
    }


def test_plan_uses_the_global_pricing_table(project: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        'pricing:\n  "gpt-5.4*": { input: 2.0, cached_input: 0.5, output: 8.0 }\n'
    )
    res = _plan(project)
    assert res.exit_code == 0, res.output
    assert "provider codex:" in res.output
    assert "estimated from the pricing table (~$)" in res.output
    assert "tokens only" not in res.output
    res = _plan(project, "--json")
    data = json.loads(res.output)
    assert data["providers"]["codex"]["cost"] == "table"
    assert data["providers"]["codex"]["unpriced_models"] == []


def test_plan_uses_the_per_provider_pricing_table(project: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        "providers:\n  codex:\n    pricing:\n"
        '      "gpt-5.4": { input: 2.0, cached_input: 0.5, output: 8.0 }\n'
    )
    res = _plan(project, "--json")
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["providers"]["codex"]["cost"] == "table"


def test_plan_names_every_unpriced_model(project: Path) -> None:
    (project / ".rayspec" / "workflows" / "wf.yaml").write_text(
        "rayspec: 1\nname: wf\nsteps:\n"
        "  - id: a\n    agent: {provider: codex, model: gpt-5.5}\n    prompt: x\n"
        "  - id: b\n    agent: {provider: codex, model: large}\n    prompt: y\n"
    )
    (project / ".rayspec" / "config.yaml").write_text(
        'pricing:\n  "gpt-5.4": { input: 2.0, cached_input: 0.5, output: 8.0 }\n'
    )
    res = _plan(project)
    assert res.exit_code == 0, res.output
    assert "tokens only for gpt-5.5 — add pricing.gpt-5.5 for estimates" in res.output
    res = _plan(project, "--json")
    codex = json.loads(res.output)["providers"]["codex"]
    assert codex["cost"] == "none" and codex["unpriced_models"] == ["gpt-5.5"]
    assert codex["priced_models"] == ["gpt-5.4"]


def test_plan_reports_a_broken_pricing_table(project: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text('pricing:\n  "gpt-5.4": { input: -1 }\n')
    res = _plan(project)
    assert res.exit_code == 0, res.output
    assert "pricing table invalid" in res.output
    assert "pricing.gpt-5.4: missing price field(s)" in res.output
    res = _plan(project, "--json")
    codex = json.loads(res.output)["providers"]["codex"]
    assert codex["cost"] == "none" and "missing price field(s)" in codex["pricing_error"]


def test_plan_reports_null_disabled_models_without_a_nudge(project: Path) -> None:
    """A ``null`` entry is a deliberate opt-out, not a missing price."""
    (project / ".rayspec" / "config.yaml").write_text('pricing:\n  "gpt-5.4": null\n')
    res = _plan(project)
    assert res.exit_code == 0, res.output
    assert "pricing disabled (null) for gpt-5.4" in res.output
    assert "add pricing.gpt-5.4" not in res.output
    res = _plan(project, "--json")
    codex = json.loads(res.output)["providers"]["codex"]
    assert codex["cost"] == "none"
    assert codex["disabled_models"] == ["gpt-5.4"]
    assert codex["unpriced_models"] == [] and codex["priced_models"] == []


def test_plan_uses_the_provider_table_when_the_global_one_is_broken(project: Path) -> None:
    (project / ".rayspec" / "config.yaml").write_text(
        "providers:\n  codex:\n    pricing:\n"
        '      "gpt-5.4": { input: 2.0, cached_input: 0.5, output: 8.0 }\n'
        'pricing:\n  "gpt-5.4": { input: -1 }\n'
    )
    res = _plan(project)
    assert res.exit_code == 0, res.output
    assert "estimated from the pricing table (~$)" in res.output
    assert "pricing table invalid" in res.output
    assert "add pricing.gpt-5.4" not in res.output
    res = _plan(project, "--json")
    codex = json.loads(res.output)["providers"]["codex"]
    assert codex["cost"] == "table" and codex["priced_models"] == ["gpt-5.4"]
    assert "missing price field(s)" in codex["pricing_error"]
