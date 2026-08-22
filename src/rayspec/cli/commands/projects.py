# SPDX-License-Identifier: Apache-2.0
"""`rayspec projects add|list|remove` — registered projects in ``~/.rayspec/config.yaml``.

Boundary: CLI presentation only; logic lives in :mod:`rayspec.workspace.registry`.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    console,
    fail,
    print_json,
    resolve_output,
)
from rayspec.config import rayspec_home
from rayspec.errors import RayspecError
from rayspec.workspace.registry import add_project, list_projects, remove_project


def register(app: typer.Typer) -> None:
    projects = typer.Typer(
        help="Register projects usable with --repo <name>.", no_args_is_help=True
    )
    app.add_typer(projects, name="projects")

    @projects.command("add")
    def add(
        name: Annotated[str, typer.Argument(help="Short name used with --repo <name>.")],
        source: Annotated[str, typer.Argument(help="Local checkout path or git URL.")],
        base: Annotated[
            str | None,
            typer.Option("--base", help="Default base branch for worktrees of this project."),
        ] = None,
    ) -> None:
        """Register (or update) a project."""
        home = rayspec_home()
        try:
            replaced = add_project(home, name, source, base=base)
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return
        verb = "updated" if replaced else "registered"
        console().print(f"{verb} project {name} → {source}" + (f" (base {base})" if base else ""))

    @projects.command("list")
    def list_(json_: JsonOption = False, output: OutputOption = None) -> None:
        """List registered projects."""
        json_ = resolve_output(output, json_)
        home = rayspec_home()
        try:
            specs = list_projects(home)
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return
        if json_:
            print_json([{"name": p.name, "source": p.source, "base": p.base} for p in specs])
            return
        out = console()
        if not specs:
            out.print(
                "no registered projects (rayspec projects add <name> <source>; "
                f"{home / 'config.yaml'})"
            )
            return
        table = Table(show_edge=False, pad_edge=False)
        table.add_column("name", style="bold")
        table.add_column("source")
        table.add_column("base")
        for p in specs:
            table.add_row(p.name, p.source, p.base or "")
        out.print(table)

    @projects.command("remove")
    def remove(name: Annotated[str, typer.Argument(help="Registered project name.")]) -> None:
        """Unregister a project (its clones and worktrees are kept)."""
        home = rayspec_home()
        try:
            removed = remove_project(home, name)
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return
        if not removed:
            fail(f"no registered project named {name!r}", hint="rayspec projects list")
            return
        console().print(f"removed project {name}")
