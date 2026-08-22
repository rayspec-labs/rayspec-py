# SPDX-License-Identifier: Apache-2.0
"""`rayspec providers` — list registered providers and their capability matrix.

Boundary: CLI presentation only. Reads the registry (no SDK import, no provider instantiation).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import typer
from rich.table import Table

from rayspec.cli.commands._loader_common import (
    OutputOption,
    console,
    new_table,
    print_json,
    resolve_output,
)
from rayspec.providers.base import ProviderCapabilities, ProviderRegistration
from rayspec.providers.registry import BUILTIN_REGISTRATIONS, list_registrations

_BUILTIN_IDS = frozenset(r.id for r in BUILTIN_REGISTRATIONS)

#: Capability attributes in display order (one matrix row each, labelled by attribute name).
_CAPABILITY_ROWS: tuple[str, ...] = (
    "structured_output",
    "session_resume",
    "session_fork",
    "instructions_modes",
    "access_levels",
    "tool_groups",
    "raw_tool_names",
    "max_turns",
    "budget_usd",
    "cost_reporting",
    "effort_levels",
    "effort_aliases",
    "thinking",
    "mcp_servers",
    "env_injection",
    "images",
    "extra",
)

_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_ACCESS_ORDER = ("read-only", "workspace-write", "full")
_GROUP_ORDER = ("read", "edit", "shell", "web", "agent", "mcp")


def _ordered(values: Iterable[Any], order: tuple[str, ...]) -> list[str]:
    strs = {str(v) for v in values}
    known = [v for v in order if v in strs]
    return known + sorted(strs - set(known))


def format_capability(caps: ProviderCapabilities, name: str) -> str:
    """Render one capability cell: ✔/✘ for flags, lists for sets, ``a→b`` for aliases."""
    value = getattr(caps, name)
    if isinstance(value, bool):
        return "✔" if value else "✘"
    if isinstance(value, Mapping):
        return ", ".join(f"{k}→{v}" for k, v in sorted(value.items())) or "—"
    if isinstance(value, frozenset | set):
        if name == "effort_levels":
            items = _ordered(value, _EFFORT_ORDER)
        elif name == "access_levels":
            items = _ordered(value, _ACCESS_ORDER)
        elif name == "tool_groups":
            items = _ordered(value, _GROUP_ORDER)
        else:
            items = sorted(str(v) for v in value)
        return " ".join(items) if items else "—"
    return str(value)


def registration_to_dict(reg: ProviderRegistration) -> dict[str, Any]:
    """JSON shape used by ``--json``: id, display_name, builtin, capabilities."""
    return {
        "id": reg.id,
        "display_name": reg.display_name,
        "builtin": reg.id in _BUILTIN_IDS,
        "capabilities": reg.capabilities.to_dict(),
    }


def render_tables(regs: list[ProviderRegistration]) -> tuple[Table, Table]:
    """Build the registry table and the transposed capability matrix (rows = capabilities)."""
    listing = new_table(title="Providers")
    listing.add_column("id", style="bold")
    listing.add_column("name")
    listing.add_column("source")
    for reg in regs:
        listing.add_row(reg.id, reg.display_name, "builtin" if reg.id in _BUILTIN_IDS else "plugin")

    matrix = new_table(title="Capabilities")
    matrix.add_column("capability", style="bold", no_wrap=True)
    for reg in regs:
        matrix.add_column(reg.id, justify="center", overflow="fold")
    for attr in _CAPABILITY_ROWS:
        matrix.add_row(attr, *(format_capability(reg.capabilities, attr) for reg in regs))
    return listing, matrix


def register(app: typer.Typer) -> None:
    @app.command()
    def providers(
        json_: bool = typer.Option(
            False, "--json", help="Print registrations and capabilities as JSON."
        ),
        output: OutputOption = None,
    ) -> None:
        """List registered providers and their declared capability matrix."""
        json_ = resolve_output(output, json_)
        regs = list_registrations()
        if json_:
            print_json([registration_to_dict(r) for r in regs])
            return
        # the shared console: terminal width on a terminal, a fixed wide one when redirected,
        # so a piped capability matrix does not fold differently than the one on screen
        out = console()
        listing, matrix = render_tables(regs)
        out.print(listing)
        out.print(matrix)
