# SPDX-License-Identifier: Apache-2.0
"""`rayspec workflows` — list discovered workflows (project ``.rayspec/`` + ``~/.rayspec/``)."""

from __future__ import annotations

import json

import typer
from rich.table import Table

from rayspec.cli._docs import docs_url
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    console,
    make_context,
    resolve_output,
    short_path,
)
from rayspec.loader import discover_workflows

#: Printed by ``workflows`` and ``validate`` when a project has no workflow yet.
EMPTY_PROJECT_HINT = (
    "run `rayspec init` to scaffold a first workflow, or create .rayspec/workflows/<name>.yaml "
    f"— examples: {docs_url('docs/examples.md')}"
)


def register(app: typer.Typer) -> None:
    @app.command()
    def workflows(
        root: RootOption = None, json_: JsonOption = False, output: OutputOption = None
    ) -> None:
        """List workflows from .rayspec/workflows/ and ~/.rayspec/workflows/."""
        json_ = resolve_output(output, json_)
        ctx = make_context(root)
        refs = discover_workflows(ctx.project_root, home=ctx.home)
        if json_:
            typer.echo(
                json.dumps(
                    [
                        {
                            "name": r.name,
                            "scope": r.scope,
                            "description": r.description,
                            "path": str(r.path),
                            "error": r.error,
                        }
                        for r in refs
                    ],
                    indent=2,
                )
            )
            return
        out = console()
        if not refs:
            out.print(
                f"no workflows found under {ctx.project_root / '.rayspec' / 'workflows'} "
                f"or {ctx.home / 'workflows'}"
            )
            out.print(f"[dim]hint: {EMPTY_PROJECT_HINT}[/dim]", highlight=False)
            return
        table = Table(title=None, show_edge=False, pad_edge=False)
        table.add_column("name", style="bold")
        table.add_column("scope")
        table.add_column("description")
        table.add_column("path", style="dim")
        broken: list[tuple[str, str]] = []
        for r in refs:
            if r.error is None:
                desc = r.description
            else:
                desc = "[red](parse error — see rayspec validate)[/red]"
                broken.append((r.name, r.error))
            table.add_row(r.name, r.scope, desc, short_path(r.path, ctx))
        out.print(table)
        for name, error in broken:
            first = error.splitlines()[0] if error else "cannot load"
            first = first.replace(str(ctx.project_root) + "/", "").replace(
                str(ctx.home), "~/.rayspec"
            )
            out.print(f"[red]error:[/red] {name}: {first}", highlight=False)
