# SPDX-License-Identifier: Apache-2.0
"""`rayspec worktrees list|clean` — inspect and remove ``rayspec/*`` worktrees.

Boundary: CLI presentation only; logic lives in :mod:`rayspec.workspace.worktrees`.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    console,
    fail,
    new_table,
    print_json,
    resolve_output,
)
from rayspec.config import load_config, rayspec_home
from rayspec.errors import RayspecError
from rayspec.workspace.project import Project, discover_project, project_from_root
from rayspec.workspace.repos import resolve_source
from rayspec.workspace.worktrees import (
    WorktreeInfo,
    clean_worktrees,
    list_worktrees,
    parse_age,
)

RepoOption = Annotated[
    str | None,
    typer.Option(
        "--repo",
        help="Project to inspect: local path, registered project name or git URL "
        "(default: the project containing --root / the cwd).",
        show_default=False,
    ),
]


def _fmt_age(age: timedelta | None) -> str:
    if age is None:
        return "?"
    secs = int(age.total_seconds())
    if secs < 3600:
        return f"{max(secs // 60, 0)}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def worktree_to_dict(info: WorktreeInfo) -> dict[str, Any]:
    """JSON shape used by ``--json``."""
    age = info.age
    return {
        "path": str(info.path),
        "branch": info.branch,
        "head_sha": info.head_sha,
        "created_at": info.created_at.isoformat() if info.created_at else None,
        "age_s": int(age.total_seconds()) if age is not None else None,
        "dirty": info.dirty,
        "merged": info.merged,
        "prunable": info.prunable,
        "locked": info.locked,
    }


def _project(root: Path | None, repo: str | None) -> Project:
    home = rayspec_home()
    if repo is not None:
        config = load_config(root, home=home)
        source = resolve_source(repo, config, home=home, fetch=False)
        if source.kind == "url":
            return Project(root=source.root, slug=source.slug, name=source.name, is_git=True)
        return project_from_root(source.root)
    project = project_from_root(root) if root is not None else discover_project()
    if not project.is_git:
        fail(
            f"{project.root} is not a git repository",
            hint="run inside a git checkout, pass --root, or --repo <path|name|url>",
        )
    return project


def _state(info: WorktreeInfo) -> str:
    flags = []
    if info.prunable:
        flags.append("[red]gone[/red]")
    if info.dirty:
        flags.append("[yellow]dirty[/yellow]")
    flags.append("[green]merged[/green]" if info.merged else "unmerged")
    if info.locked:
        flags.append("locked")
    return " ".join(flags)


def register(app: typer.Typer) -> None:
    worktrees = typer.Typer(help="List and clean rayspec worktrees.", no_args_is_help=True)
    app.add_typer(worktrees, name="worktrees")

    @worktrees.command("list")
    def list_(
        *,
        root: RootOption = None,
        repo: RepoOption = None,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """List worktrees on rayspec/* branches (age, dirty/merged state)."""
        json_ = resolve_output(output, json_)
        try:
            project = _project(root, repo)
            infos = list_worktrees(project)
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return
        if json_:
            print_json([worktree_to_dict(i) for i in infos])
            return
        out = console()
        if not infos:
            out.print(f"no rayspec worktrees for {project.slug}")
            return
        table = new_table()
        table.add_column("branch", style="bold")
        table.add_column("age")
        table.add_column("state")
        table.add_column("path", style="dim")
        for info in infos:
            table.add_row(info.branch, _fmt_age(info.age), _state(info), str(info.path))
        out.print(table)

    @worktrees.command("clean")
    def clean(
        *,
        root: RootOption = None,
        repo: RepoOption = None,
        older_than: Annotated[
            str | None,
            typer.Option("--older-than", help="Only worktrees older than this (e.g. 7d, 12h)."),
        ] = None,
        merged: Annotated[
            bool, typer.Option("--merged", help="Only worktrees merged into origin/HEAD (or HEAD).")
        ] = False,
        merged_into: Annotated[
            str | None,
            typer.Option(
                "--merged-into",
                help="Ref that decides 'merged' (default: origin/HEAD, else HEAD).",
                show_default=False,
            ),
        ] = None,
        force: Annotated[
            bool,
            typer.Option(
                "--force",
                help="Also remove unmerged (committed work), dirty and locked worktrees.",
            ),
        ] = False,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Report what would be removed.")
        ] = False,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """Remove rayspec worktrees and their branches (git worktree remove + git branch -D).

        Safe by default: only merged, clean, unlocked worktrees go; everything else is listed
        as skipped with the reason (--force overrides).
        """
        json_ = resolve_output(output, json_)
        try:
            age = parse_age(older_than) if older_than is not None else None
        except ValueError as exc:
            fail(str(exc))
            return
        try:
            project = _project(root, repo)
            report = clean_worktrees(
                project,
                older_than=age,
                merged_only=merged,
                force=force,
                dry_run=dry_run,
                merged_into=merged_into,
                home=rayspec_home(),  # drop the lock files of removed worktrees
            )
        except RayspecError as exc:
            fail(str(exc), hint=exc.hint)
            return
        if json_:
            print_json(
                {
                    "dry_run": dry_run,
                    "removed": [worktree_to_dict(i) for i in report.removed],
                    "skipped": [
                        {**worktree_to_dict(i), "reason": reason} for i, reason in report.skipped
                    ],
                }
            )
            return
        out = console()
        verb = "would remove" if dry_run else "removed"
        for info in report.removed:
            out.print(f"{verb} {info.branch} [dim]{info.path}[/dim]")
        for info, reason in report.skipped:
            out.print(f"[yellow]skipped[/yellow] {info.branch}: {reason}")
        if not report.removed and not report.skipped:
            out.print(f"no rayspec worktrees for {project.slug}")
        elif not report.removed:
            out.print("nothing removed")
