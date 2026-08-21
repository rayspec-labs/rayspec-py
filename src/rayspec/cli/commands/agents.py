# SPDX-License-Identifier: Apache-2.0
"""`rayspec agents` — list named agent files (``.rayspec/agents/`` + ``~/.rayspec/agents/``).

Boundary: read-only listing. Each file is parsed on its own (no workflow context), then its
``provider`` / ``model`` / ``effort`` are resolved against ``config.yaml`` the way the loader does
for ``plan`` (``@alias`` may pin the provider; tiers resolve per provider), so the table shows the
provider an agent will actually run on.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    console,
    make_context,
    resolve_output,
    short_path,
)
from rayspec.config import TIER_NAMES, Config
from rayspec.errors import RayspecError
from rayspec.loader import discover_agents
from rayspec.loader.loader import DEFAULT_MODEL_TIER
from rayspec.loader.yaml import load_yaml
from rayspec.schema import AgentDef, parse_agent_def


@dataclass(frozen=True, slots=True)
class ResolvedFields:
    """What an agent file resolves to under the merged config (the ``resolved`` JSON block).

    ``via`` names the alias (``@fast``) or tier (``medium``) the model came from, ``None`` for a
    literal model id; ``model`` is ``None`` when the tier has no model for the provider (the
    provider default is used) or when ``problem`` is set (unknown alias, alias/provider conflict:
    the loader refuses such an agent, so no model or effort would run). ``provider_from``
    says who chose the provider: the ``agent`` file, a pinning ``alias`` or the config ``default``.
    """

    provider: str
    model: str | None
    effort: str | None
    via: str | None
    provider_from: str = "agent"
    problem: str | None = None


def resolve_fields(agent: AgentDef, config: Config) -> ResolvedFields:
    """Resolve ``provider``/``model``/``effort`` like the loader: ``agent.provider`` else the
    config default; ``@alias`` → alias model (+ pinned provider, default effort); tier → the
    provider's tier model (+ default effort); a literal model passes through."""
    provider = agent.provider or config.default_provider
    origin = "agent" if agent.provider else "default"
    raw_model = agent.model if agent.model is not None else DEFAULT_MODEL_TIER
    effort = agent.effort
    if raw_model.startswith("@"):
        alias = config.aliases.get(raw_model)
        if alias is None:
            known = ", ".join(sorted(config.aliases)) or "none configured"
            return ResolvedFields(
                provider,
                None,
                effort,
                raw_model,
                origin,
                problem=f"unknown alias {raw_model!r} (aliases: {known})",
            )
        if alias.provider and agent.provider and alias.provider != agent.provider:
            return ResolvedFields(
                provider,
                None,
                None,
                raw_model,
                origin,
                problem=f"alias {raw_model!r} pins provider {alias.provider!r} but the agent "
                f"sets provider {agent.provider!r}",
            )
        if alias.provider:
            provider, origin = alias.provider, "alias"
        return ResolvedFields(provider, alias.model, effort or alias.effort, raw_model, origin)
    if raw_model in TIER_NAMES:
        tier = config.resolve_tier(provider, raw_model)
        if tier is None:
            return ResolvedFields(provider, None, effort, raw_model, origin)
        return ResolvedFields(provider, tier.model, effort or tier.effort, raw_model, origin)
    return ResolvedFields(provider, raw_model, effort, None, origin)


def _describe(path: Path, config: Config) -> dict[str, Any]:
    try:
        data = load_yaml(path.read_text(encoding="utf-8"), source=str(path))
        agent = parse_agent_def(data or {}, source=str(path))
    except (RayspecError, OSError) as exc:
        return {
            "provider": None,
            "model": None,
            "effort": None,
            "access": None,
            "error": str(exc),
            "resolved": None,
        }
    return {
        "provider": agent.provider,
        "model": agent.model,
        "effort": agent.effort,
        "access": agent.access,
        "error": None,
        "resolved": asdict(resolve_fields(agent, config)),
    }


def _provider_cell(row: dict[str, Any]) -> str:
    resolved = row["resolved"]
    provider = escape(resolved["provider"])
    if resolved["provider_from"] == "alias":
        return f"{provider} [dim](via {escape(resolved['via'])})[/dim]"
    if resolved["provider_from"] == "default":
        return f"{provider} [dim](default)[/dim]"
    return provider


def _model_cell(row: dict[str, Any]) -> str:
    resolved = row["resolved"]
    if resolved["problem"]:
        return f"[red]{escape(resolved['problem'])}[/red]"
    via = f" [dim]({escape(resolved['via'])})[/dim]" if resolved["via"] else ""
    model = escape(resolved["model"]) if resolved["model"] else "[dim](provider default)[/dim]"
    return model + via


def register(app: typer.Typer) -> None:
    @app.command()
    def agents(
        root: RootOption = None, json_: JsonOption = False, output: OutputOption = None
    ) -> None:
        """List named agent files with their resolved provider/model (aliases and tiers applied)."""
        json_ = resolve_output(output, json_)
        ctx = make_context(root)
        refs = discover_agents(ctx.project_root, home=ctx.home)
        rows = [
            {"name": r.name, "scope": r.scope, "path": str(r.path), **_describe(r.path, ctx.config)}
            for r in refs
        ]
        if json_:
            typer.echo(json.dumps(rows, indent=2))
            return
        out = console()
        if not rows:
            out.print(
                f"no agent files found under {ctx.project_root / '.rayspec' / 'agents'} "
                f"or {ctx.home / 'agents'}"
            )
            return
        table = Table(show_edge=False, pad_edge=False)
        for col in ("name", "scope", "provider", "model", "effort", "access", "path"):
            table.add_column(col, style="bold" if col == "name" else None)
        for row in rows:
            if row["error"]:
                table.add_row(
                    escape(row["name"]),
                    row["scope"],
                    f"[red]error: {escape(row['error'])}[/red]",
                    "",
                    "",
                    "",
                    short_path(Path(row["path"]), ctx),
                )
            else:
                table.add_row(
                    escape(row["name"]),
                    row["scope"],
                    _provider_cell(row),
                    _model_cell(row),
                    row["resolved"]["effort"] or "",
                    row["access"] or "",
                    short_path(Path(row["path"]), ctx),
                )
        out.print(table)


__all__ = ["ResolvedFields", "register", "resolve_fields"]
