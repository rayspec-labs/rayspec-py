"""`rayspec providers` table and --json output."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import _loader_common as loader_common
from rayspec.providers import registry
from rayspec.providers.base import ProviderCapabilities, ProviderRegistration
from rayspec.providers.capabilities import CLAUDE_CAPABILITIES, CODEX_CAPABILITIES
from rayspec.providers.stub import StubProvider


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch):
    monkeypatch.setattr(registry, "entry_points", lambda group: [])
    registry.reset_registry()
    yield
    registry.reset_registry()


def test_providers_table_lists_builtins_with_capability_matrix():
    result = CliRunner().invoke(app, ["providers"])
    assert result.exit_code == 0, result.output
    out = result.output
    for needle in ("claude", "codex", "stub", "Claude Agent SDK", "OpenAI Codex SDK"):
        assert needle in out
    assert "✔" in out and "✘" in out
    # capability rows are present (transposed matrix: one row per capability)
    for row in ("structured_output", "max_turns", "tool_groups", "effort_levels", "mcp_servers"):
        assert row in out
    assert "enforced" in out
    assert "minimal→low" in out  # alias rendering


def test_providers_json_dumps_registrations_and_capabilities():
    result = CliRunner().invoke(app, ["providers", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    by_id = {item["id"]: item for item in data}
    assert set(by_id) == {"claude", "codex", "stub"}
    assert by_id["claude"]["display_name"] == "Claude Agent SDK"
    assert by_id["claude"]["capabilities"] == CLAUDE_CAPABILITIES.to_dict()
    assert by_id["codex"]["capabilities"] == CODEX_CAPABILITIES.to_dict()
    assert by_id["codex"]["capabilities"]["max_turns"] is False
    assert by_id["claude"]["builtin"] is True


def test_providers_includes_registered_third_party():
    caps = ProviderCapabilities(
        structured_output="none",
        session_resume=False,
        session_fork=False,
        instructions_modes=frozenset({"append"}),
        access_levels=frozenset(),
        tool_groups=frozenset(),
        raw_tool_names=False,
        max_turns=False,
        budget_usd=False,
        cost_reporting=False,
        effort_levels=frozenset(),
    )
    registry.register(
        ProviderRegistration(
            id="acme", display_name="ACME", capabilities=caps, factory=StubProvider
        )
    )
    result = CliRunner().invoke(app, ["providers", "--json"])
    data = json.loads(result.output)
    acme = next(item for item in data if item["id"] == "acme")
    assert acme["builtin"] is False and acme["display_name"] == "ACME"
    table = CliRunner().invoke(app, ["providers"])
    assert "acme" in table.output and "ACME" in table.output


def test_providers_table_uses_terminal_width_without_truncating_labels(monkeypatch):
    """On a terminal the matrix folds into the terminal's width, labels intact."""
    monkeypatch.setenv("COLUMNS", "60")
    monkeypatch.setattr(loader_common, "stdout_is_tty", lambda: True)
    result = CliRunner().invoke(app, ["providers"])
    assert result.exit_code == 0, result.output
    assert "instructions_modes" in result.output  # never ellipsised on narrow terminals
    assert max(len(line) for line in result.output.splitlines()) <= 60


def test_providers_table_is_the_same_width_whoever_redirects_it(monkeypatch):
    """Redirected, the listing does not depend on the reader's terminal: piping `rayspec
    providers` into a file from an 80-column shell and from a wide one gives the same bytes."""
    monkeypatch.setenv("COLUMNS", "60")
    narrow = CliRunner().invoke(app, ["providers"])
    monkeypatch.setenv("COLUMNS", "200")
    wide = CliRunner().invoke(app, ["providers"])
    assert narrow.exit_code == 0 and wide.exit_code == 0
    assert narrow.stdout == wide.stdout
