"""``rayspec plugins`` — the inventory a user reads when an unexpected command shows up."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from .conftest import InstallPlugin

COMMAND = """
import typer


def register(app: typer.Typer) -> None:
    @app.command("acme-lint")
    def acme_lint() -> None:
        \"\"\"Lint the acme way.\"\"\"
"""

SINK = """
from rayspec.registry import SinkRegistration


class MemorySink:
    def __init__(self, context):
        self.context = context

    async def emit(self, event): pass
    async def emit_stream(self, step_path, record): pass
    async def aclose(self): pass


SINK = SinkRegistration("memory", "Memory sink", MemorySink)
"""

BROKEN = """
raise RuntimeError("boom at import time")
"""


def _invoke(args: list[str]):
    from rayspec.cli.app import build_app

    return CliRunner().invoke(build_app(), args)


def test_without_plugins_the_listing_is_empty_but_names_the_builtin_ids() -> None:
    result = _invoke(["plugins"])
    assert result.exit_code == 0
    assert "no plugins installed" in result.output
    assert "file" in result.output  # the builtin store id config may name

    payload = json.loads(_invoke(["plugins", "--json"]).output)
    assert payload["plugins"] == []
    assert [entry["id"] for entry in payload["registered"]["stores"]] == ["file"]
    assert [entry["id"] for entry in payload["registered"]["approvals"]] == ["console"]


def test_an_installed_plugin_is_listed_with_its_distribution(install_plugin: InstallPlugin) -> None:
    install_plugin(
        "acme-rayspec",
        version="1.4.0",
        modules={"acme_cmd": COMMAND, "acme_sink": SINK},
        entry_points={
            "rayspec.cli_plugins": {"acme": "acme_cmd:register"},
            "rayspec.sinks": {"memory": "acme_sink:SINK"},
        },
    )
    payload = json.loads(_invoke(["plugins", "--json"]).output)
    rows = {(row["group"], row["name"]): row for row in payload["plugins"]}
    command_row = rows[("rayspec.cli_plugins", "acme")]
    assert command_row["distribution"] == "acme-rayspec"
    assert command_row["version"] == "1.4.0"
    assert command_row["status"] == "ok"
    assert command_row["detail"] == "adds acme-lint"
    assert rows[("rayspec.sinks", "memory")]["status"] == "ok"
    assert [entry["id"] for entry in payload["registered"]["sinks"]] == [
        "console",
        "json",
        "quiet",
        "null",
        "memory",
    ]

    output = _invoke(["plugins"]).output
    assert "acme-rayspec 1.4.0" in output


def test_a_skipped_plugin_says_why(install_plugin: InstallPlugin) -> None:
    install_plugin(
        "acme-rayspec",
        modules={"acme_broken": BROKEN},
        entry_points={
            "rayspec.cli_plugins": {"acme": "acme_broken:register"},
            "rayspec.sinks": {"memory": "acme_broken:SINK"},
        },
    )
    with pytest.warns(RuntimeWarning):
        payload = json.loads(_invoke(["plugins", "--json"]).output)
    rows = {(row["group"], row["name"]): row for row in payload["plugins"]}
    assert rows[("rayspec.cli_plugins", "acme")]["status"] == "skipped"
    assert "boom at import time" in rows[("rayspec.cli_plugins", "acme")]["detail"]
    assert rows[("rayspec.sinks", "memory")]["status"] == "skipped"
    assert "boom at import time" in rows[("rayspec.sinks", "memory")]["detail"]


PARTLY_REFUSED = """
import typer


def register(app: typer.Typer) -> None:
    @app.command("acme-lint")
    def acme_lint() -> None:
        \"\"\"Lint the acme way.\"\"\"

    @app.command("run")
    def run() -> None:
        \"\"\"Shadow the builtin.\"\"\"
"""


def test_a_partially_refused_plugin_names_the_dropped_command(
    install_plugin: InstallPlugin,
) -> None:
    """ "Why is my plugin's command missing" is the other half of what this command answers."""
    install_plugin(
        "acme-rayspec",
        modules={"acme_partly": PARTLY_REFUSED},
        entry_points={"rayspec.cli_plugins": {"acme": "acme_partly:register"}},
    )
    with pytest.warns(RuntimeWarning, match="'run'"):
        payload = json.loads(_invoke(["plugins", "--json"]).output)
    row = {(r["group"], r["name"]): r for r in payload["plugins"]}[("rayspec.cli_plugins", "acme")]
    assert row["status"] == "ok"
    assert row["detail"] == "adds acme-lint; dropped run (already provided)"


DUPLICATE = """
from rayspec.registry import SinkRegistration


class Dup:
    def __init__(self, context): self.context = context

    async def emit(self, event): pass
    async def emit_stream(self, step_path, record): pass
    async def aclose(self): pass


SINK = SinkRegistration("dup", "Duplicate sink", Dup)
"""


def test_one_of_two_distributions_claiming_an_id_is_shown_as_skipped(
    install_plugin: InstallPlugin,
) -> None:
    """Both rows are listed, and the listing says which one actually provides the id."""
    install_plugin(
        "aaa-rayspec",
        modules={"aaa_dup": DUPLICATE},
        entry_points={"rayspec.sinks": {"dup": "aaa_dup:SINK"}},
    )
    install_plugin(
        "zzz-rayspec",
        modules={"zzz_dup": DUPLICATE},
        entry_points={"rayspec.sinks": {"dup": "zzz_dup:SINK"}},
    )
    with pytest.warns(RuntimeWarning, match="already provided by another distribution"):
        payload = json.loads(_invoke(["plugins", "--json"]).output)
    rows = [row for row in payload["plugins"] if row["name"] == "dup"]
    assert len(rows) == 2
    assert sorted(row["status"] for row in rows) == ["ok", "skipped"]
