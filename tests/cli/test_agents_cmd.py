"""`rayspec agents` resolves @aliases and tiers the way `plan` does."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

runner = CliRunner()

CONFIG = """\
default_provider: claude
tiers:
  claude: { small: haiku, medium: sonnet, large: opus }
  codex:
    small: { model: gpt-5.4, effort: low }
    medium: gpt-5.4
aliases:
  "@fast": { provider: codex, model: gpt-5.4, effort: low }
  "@deep": { model: opus, effort: high }
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    root = tmp_path / "proj"
    agents = root / ".rayspec" / "agents"
    agents.mkdir(parents=True)
    (root / ".rayspec" / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (agents / "codex_reviewer.yaml").write_text('model: "@fast"\naccess: read-only\n')
    (agents / "reviewer.yaml").write_text("provider: claude\nmodel: medium\neffort: medium\n")
    (agents / "deep.yaml").write_text('model: "@deep"\n')
    (agents / "plain.yaml").write_text("access: read-only\n")
    (agents / "lost.yaml").write_text('model: "@nope"\n')
    (agents / "bare.yaml").write_text("provider: plugin_x\nmodel: large\n")
    return root


#: The agents the ``project`` fixture scaffolds.
AGENTS = frozenset({"bare", "codex_reviewer", "deep", "lost", "plain", "reviewer"})


def _rows(output: str, *, expected: frozenset[str] = AGENTS) -> dict[str, str]:
    """``{agent name: the whole line}``. Tables carry no borders, so a row is keyed by its first
    column — an agent name is an identifier and never contains a space.

    Only lines whose first token is one of ``expected`` count, so the header cannot pass for a
    row, and a cell folded onto a second line cannot invent one. The set is then compared, which
    is what makes a row that went missing fail here rather than three assertions later.
    """
    rows: dict[str, str] = {}
    for line in output.splitlines():
        first = line.split()
        if first and first[0] in expected:
            assert first[0] not in rows, f"{first[0]} listed twice: {line!r}"
            rows[first[0]] = line
    assert set(rows) == set(expected), f"listed {sorted(rows)}, expected {sorted(expected)}"
    return rows


def test_agents_table_shows_the_resolved_provider_and_model(project: Path) -> None:
    res = runner.invoke(app, ["agents", "--root", str(project)])
    assert res.exit_code == 0, res.output
    rows = _rows(res.output)
    assert "(default: claude)" not in res.output
    assert "codex (via @fast)" in rows["codex_reviewer"]
    assert "gpt-5.4 (@fast)" in rows["codex_reviewer"]
    assert "low" in rows["codex_reviewer"]
    assert "sonnet (medium)" in rows["reviewer"]
    # an unpinned alias keeps the agent's / default provider
    assert "claude (default)" in rows["deep"] and "opus (@deep)" in rows["deep"]
    assert "claude (default)" in rows["plain"] and "sonnet (medium)" in rows["plain"]
    assert "unknown alias" in rows["lost"] and "@nope" in rows["lost"]
    # a tier without a configured model for the provider: provider default
    assert "provider default" in rows["bare"] and "(large)" in rows["bare"]


def test_agents_json_adds_the_resolved_block_and_keeps_the_raw_fields(project: Path) -> None:
    res = runner.invoke(app, ["agents", "--json", "--root", str(project)])
    assert res.exit_code == 0, res.output
    by_name = {row["name"]: row for row in json.loads(res.output)}
    codex = by_name["codex_reviewer"]
    assert codex["provider"] is None and codex["model"] == "@fast"
    assert codex["resolved"] == {
        "provider": "codex",
        "model": "gpt-5.4",
        "effort": "low",
        "via": "@fast",
        "provider_from": "alias",
        "problem": None,
    }
    assert by_name["reviewer"]["resolved"] == {
        "provider": "claude",
        "model": "sonnet",
        "effort": "medium",
        "via": "medium",
        "provider_from": "agent",
        "problem": None,
    }
    assert by_name["plain"]["resolved"]["provider"] == "claude"
    assert by_name["plain"]["resolved"]["provider_from"] == "default"
    assert by_name["deep"]["resolved"]["provider_from"] == "default"
    assert by_name["lost"]["resolved"]["model"] is None
    assert "unknown alias" in by_name["lost"]["resolved"]["problem"]
    assert by_name["bare"]["resolved"] == {
        "provider": "plugin_x",
        "model": None,
        "effort": None,
        "via": "large",
        "provider_from": "agent",
        "problem": None,
    }


def test_agents_alias_conflict_reports_the_problem_and_no_model_or_effort(project: Path) -> None:
    """An alias that pins another provider than the agent is a problem; the row shows no
    model/effort (the loader refuses such an agent, so none would ever run)."""
    (project / ".rayspec" / "agents" / "clash.yaml").write_text(
        'provider: claude\nmodel: "@fast"\n', encoding="utf-8"
    )
    res = runner.invoke(app, ["agents", "--json", "--root", str(project)])
    assert res.exit_code == 0, res.output
    clash = {row["name"]: row for row in json.loads(res.output)}["clash"]
    assert clash["resolved"]["model"] is None and clash["resolved"]["effort"] is None
    assert "pins provider 'codex'" in clash["resolved"]["problem"]
    res = runner.invoke(app, ["agents", "--root", str(project)])
    row = _rows(res.output, expected=AGENTS | {"clash"})["clash"]
    assert "pins provider" in row and "low" not in row
