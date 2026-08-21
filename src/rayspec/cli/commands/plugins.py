# SPDX-License-Identifier: Apache-2.0
"""`rayspec plugins` — what is installed on top of rayspec, and where it comes from.

Boundary: CLI presentation only. It reports what :mod:`rayspec.cli.plugins` and
:mod:`rayspec.registry` already resolved and imports nothing extra — the point of the command is
to answer "why is there a command/store/sink I did not write?" without running anything.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from rayspec.cli._docs import docs_url
from rayspec.cli.plugins import CLI_ENTRY_POINT_GROUP, InstalledPlugin, installed_plugins

#: Where a reader is sent to write one of these.
PLUGINS_DOCS = docs_url("docs/extending.md")


def builtin_ids() -> dict[str, list[dict[str, str]]]:
    """The ids config can select without installing anything, per extension kind."""
    from rayspec import registry

    return {
        "stores": [{"id": r.id, "name": r.display_name} for r in registry.list_stores()],
        "sinks": [{"id": r.id, "name": r.display_name} for r in registry.list_sinks()],
        "approvals": [{"id": r.id, "name": r.display_name} for r in registry.list_approvals()],
    }


def plugin_to_dict(plugin: InstalledPlugin) -> dict[str, Any]:
    """JSON shape used by ``--json``: one installed entry point and what became of it."""
    return {
        "group": plugin.group,
        "name": plugin.name,
        "value": plugin.value,
        "distribution": plugin.distribution,
        "version": plugin.version,
        "status": plugin.status,
        "detail": plugin.detail,
    }


def render_table(plugins: list[InstalledPlugin]) -> Table:
    """The listing: one row per installed entry point, grouped by entry-point group."""
    table = Table(title="Plugins", show_lines=False)
    table.add_column("group", style="bold", no_wrap=True)
    table.add_column("name")
    table.add_column("from")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    for plugin in plugins:
        source = plugin.distribution or plugin.value
        if plugin.version:
            source = f"{source} {plugin.version}"
        style = "red" if plugin.status == "skipped" else ""
        table.add_row(plugin.group, plugin.name, source, plugin.status, plugin.detail, style=style)
    return table


def render_builtins(builtins: dict[str, list[dict[str, str]]]) -> Table:
    """The ids that always exist (what ``extensions:`` in ``config.yaml`` may name)."""
    table = Table(title="Registered ids", show_lines=False)
    table.add_column("kind", style="bold", no_wrap=True)
    table.add_column("ids")
    for kind, entries in builtins.items():
        table.add_row(kind, ", ".join(entry["id"] for entry in entries))
    return table


def register(app: typer.Typer) -> None:
    @app.command()
    def plugins(
        json_: bool = typer.Option(False, "--json", help="Print the listing as JSON."),
    ) -> None:
        """List installed rayspec plugins: commands, stores, sinks, approvals and providers."""
        builtins = builtin_ids()  # also forces discovery, so a skipped plugin is reported
        installed = installed_plugins()
        if json_:
            payload = {
                "plugins": [plugin_to_dict(plugin) for plugin in installed],
                "registered": builtins,
            }
            typer.echo(json.dumps(payload, indent=2))
            return
        console = Console()
        if installed:
            console.print(render_table(installed))
        else:
            console.print(
                f"no plugins installed (nothing publishes {CLI_ENTRY_POINT_GROUP} or a "
                f"rayspec.stores/sinks/approvals/providers entry point)",
                markup=False,
            )
        console.print(render_builtins(builtins))
        console.print(f"writing one: {PLUGINS_DOCS}", markup=False)
