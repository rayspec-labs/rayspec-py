# SPDX-License-Identifier: Apache-2.0
"""``rayspec trust add|list|remove|check`` — the workflows this checkout may run.

Boundary: CLI presentation only; the file, its format and the hash comparison live in
:mod:`rayspec.policy.trust`. ``rayspec trust check`` is the piece a scheduled job puts in front
of ``rayspec run``: it exits 1 when a workflow is not listed at its current hash, so an edited
workflow stops the schedule instead of running unreviewed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from rayspec.cli.commands._loader_common import (
    Context,
    JsonOption,
    OutputOption,
    RootOption,
    console,
    error_entries,
    fail,
    make_context,
    new_table,
    print_json,
    resolve_output,
    short_path,
)
from rayspec.errors import RayspecError
from rayspec.loader import ResolvedWorkflow, discover_workflows, load_workflow
from rayspec.policy import TrustEntry, TrustStore, trusted_path

#: What ``trust list`` says about an entry whose workflow no longer hashes to what was trusted.
STATUS_CURRENT = "current"
STATUS_CHANGED = "changed"
STATUS_MISSING = "missing"


def _resolve(target: str, ctx: Context) -> ResolvedWorkflow:
    """Load one workflow by name or path, reporting load errors the way every command does."""
    try:
        return load_workflow(
            target, project_root=ctx.project_root, home=ctx.home, config=ctx.config
        )
    except RayspecError as exc:
        fail("\n".join(error_entries(exc)), hint=exc.hint)
        raise AssertionError("unreachable") from None  # pragma: no cover


def _status(entry: TrustEntry, ctx: Context, store: TrustStore) -> str:
    """Whether the workflow behind ``entry`` still hashes to the digest that was trusted.

    The comparison is the store's, not a second one written here: this listing is what a person
    reads to predict whether ``rayspec run`` will pass, so it must answer the question the gate
    answers, digest algorithm and all.
    """
    try:
        resolved = load_workflow(
            entry.workflow, project_root=ctx.project_root, home=ctx.home, config=ctx.config
        )
    except RayspecError:
        return STATUS_MISSING
    return STATUS_CURRENT if store.problem_for(resolved) is None else STATUS_CHANGED


def register(app: typer.Typer) -> None:
    trust = typer.Typer(
        help="Allow-list workflows by their resolved hash (.rayspec/trusted.yaml).",
        no_args_is_help=True,
    )
    app.add_typer(trust, name="trust")

    @trust.command("add")
    def add(
        workflows: Annotated[
            list[str], typer.Argument(help="Workflow names or paths to trust.", show_default=False)
        ],
        root: RootOption = None,
    ) -> None:
        """Record a workflow's current hash as trusted.

        The hash covers every file that contributed to the workflow — included bodies, agent
        files and prompt files — so review what you are trusting, not only the entry document.
        """
        ctx = make_context(root)
        store = TrustStore.load(ctx.project_root)
        out = console()
        for target in workflows:
            resolved = _resolve(target, ctx)
            store, replaced = store.add(resolved)
            verb = "updated" if replaced else "trusted"
            out.print(f"{verb} {resolved.workflow.name} ({resolved.label}) {resolved.hash[:12]}")
        store.save()

    @trust.command("list")
    def list_(
        root: RootOption = None, json_: JsonOption = False, output: OutputOption = None
    ) -> None:
        """List the trusted workflows and whether each one still matches its hash."""
        json_ = resolve_output(output, json_)
        ctx = make_context(root)
        store = TrustStore.load(ctx.project_root)
        rows: list[dict[str, Any]] = [
            {
                "workflow": entry.workflow,
                "hash": entry.hash,
                "added": entry.added,
                "status": _status(entry, ctx, store),
            }
            for entry in store.entries
        ]
        if json_:
            print_json(rows)
            return
        out = console()
        if not rows:
            out.print(
                f"no trusted workflows ({short_path(trusted_path(ctx.project_root), ctx)}; "
                "rayspec trust add <workflow>)"
            )
            return
        table = new_table()
        table.add_column("workflow", style="bold")
        table.add_column("hash")
        table.add_column("added")
        table.add_column("status")
        for row in rows:
            style = "green" if row["status"] == STATUS_CURRENT else "yellow"
            table.add_row(
                row["workflow"],
                str(row["hash"]).split(":", 1)[-1][:12],
                row["added"],
                f"[{style}]{row['status']}[/{style}]",
            )
        out.print(table)

    @trust.command("remove")
    def remove(
        workflows: Annotated[
            list[str],
            typer.Argument(help="Workflow names or paths to un-trust.", show_default=False),
        ],
        root: RootOption = None,
    ) -> None:
        """Drop workflows from the trust list."""
        ctx = make_context(root)
        store = TrustStore.load(ctx.project_root)
        out = console()
        for target in workflows:
            label = _label_for(target, ctx, store)
            store, removed = store.remove(label)
            if not removed:
                fail(f"{target!r} is not in the trust list", hint="rayspec trust list")
                return
            out.print(f"removed {label}")
        store.save()

    @trust.command("check")
    def check(
        workflows: Annotated[
            list[str] | None,
            typer.Argument(help="Workflows to check (default: every discovered workflow)."),
        ] = None,
        root: RootOption = None,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """Exit 0 only when every named workflow is trusted at its current hash.

        With no arguments every discovered workflow is checked — the shape a scheduled job wants
        in front of `rayspec run`. A workflow that does not load is reported as untrusted like
        any other drift (exit 1); only a name given here that does not exist is exit 2.
        """
        json_ = resolve_output(output, json_)
        ctx = make_context(root)
        store = TrustStore.load(ctx.project_root)
        named = list(workflows or [])
        known = {ref.name for ref in discover_workflows(ctx.project_root, home=ctx.home)}
        targets = named or sorted(known)
        for target in named:  # a name given on the command line has to exist
            if target not in known and not Path(target).is_file():
                _resolve(target, ctx)
        rows: list[dict[str, Any]] = []
        for target in targets:
            try:
                resolved = load_workflow(
                    target, project_root=ctx.project_root, home=ctx.home, config=ctx.config
                )
            except RayspecError as exc:
                # a workflow that does not load is not a trusted workflow, and one broken file
                # must not hide the trust status of every other workflow in the repository
                rows.append(
                    {
                        "workflow": target,
                        "name": target,
                        "hash": "",
                        "trusted": False,
                        "problem": f"does not load: {str(exc).splitlines()[0]}",
                    }
                )
                continue
            problem = store.problem_for(resolved)
            rows.append(
                {
                    "workflow": resolved.label,
                    "name": resolved.workflow.name,
                    "hash": resolved.hash,
                    "trusted": problem is None,
                    "problem": problem,
                }
            )
        if json_:
            print_json(rows)
        else:
            out = console()
            for row in rows:
                if row["trusted"]:
                    out.print(f"[green]trusted[/green] {row['name']} ({row['workflow']})")
                else:
                    out.print(
                        f"[yellow]not trusted[/yellow] {row['name']} ({row['workflow']}): "
                        f"{row['problem']}"
                    )
            if not rows:
                out.print("no workflows to check")
        if any(not row["trusted"] for row in rows):
            raise typer.Exit(code=1)


def _label_for(target: str, ctx: Context, store: TrustStore) -> str:
    """The trust-list label of ``target``: an exact entry, else the workflow's own label."""
    if store.entry_for(target) is not None:
        return target
    try:
        return load_workflow(
            target, project_root=ctx.project_root, home=ctx.home, config=ctx.config
        ).label
    except RayspecError:
        return target


__all__ = ["STATUS_CHANGED", "STATUS_CURRENT", "STATUS_MISSING", "register"]
