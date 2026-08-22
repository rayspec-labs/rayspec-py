# SPDX-License-Identifier: Apache-2.0
"""`rayspec lock [names...] [--check] [--json] [--root]` — pin what every agent resolves to.

Also the home of the shared ``--locked`` gate that ``run``, ``plan`` and ``validate`` apply
(:data:`LockedOption`, :func:`enforce_lockfile`), so the three commands cannot drift apart in
how they read the lockfile or phrase the refusal.

``model: sonnet`` is a tier, ``@fast`` is an alias and an unset ``model:`` is the provider's
default — all three mean "whatever this resolves to today". The lockfile records what that was;
``--locked`` refuses a run whose agents resolve to something else. It is on by default under
``CI``, because an unattended run silently using a different model than the one a human reviewed
is exactly the failure the lockfile exists to prevent.

Exit codes: ``0`` written / in sync · ``1`` ``--check`` found drift · ``2`` usage (unknown
workflow, a workflow that does not load, an unreadable lockfile).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from rayspec.cli.commands._loader_common import (
    Context,
    JsonOption,
    OutputOption,
    RootOption,
    console,
    error_lines,
    fail,
    make_context,
    print_json,
    resolve_output,
    short_path,
    workflow_label,
)
from rayspec.errors import RayspecError
from rayspec.limits import (
    LockEntry,
    LockfileError,
    check_locked,
    load_lockfile,
    lock_entries_for,
    locked_default,
    lockfile_path,
    merged_workflows,
    write_lockfile,
)
from rayspec.loader import ResolvedWorkflow, discover_workflows, load_workflow

LockedOption = Annotated[
    bool | None,
    typer.Option(
        "--locked/--no-locked",
        help="Refuse to proceed when an agent resolves differently than .rayspec/rayspec.lock "
        "pins it. Default: on under CI, off otherwise.",
        show_default=False,
    ),
]


def locked_enabled(locked: bool | None, environ: Mapping[str, str] | None = None) -> bool:
    """Whether the lockfile is enforced: the flag when given, else the ``CI`` default."""
    if locked is not None:
        return locked
    return locked_default(environ if environ is not None else os.environ)


def enforce_lockfile(
    ctx: Context,
    resolved: ResolvedWorkflow,
    *,
    locked: bool | None,
    project_root: Path | None = None,
    json_mode: bool = False,
) -> None:
    """Exit 2 when ``--locked`` is in force and the workflow does not match the lockfile.

    ``project_root`` is the project the workflow was LOADED from — with ``--repo`` that is the
    prepared checkout, not the directory the command was typed in, and checking the caller's
    lockfile there would validate a file that has nothing to do with the code being run.

    A missing lockfile is refused when ``--locked`` was passed: the flag is a promise that the
    models were pinned, and "there is nothing to check" must not read as "everything is fine".
    The CI *default* is different — it may not break a project that never opted in, so with no
    flag and no lockfile there is simply nothing to enforce.
    """
    if not locked_enabled(locked):
        return
    root = project_root if project_root is not None else ctx.project_root
    try:
        lockfile = load_lockfile(root)
    except LockfileError as exc:
        fail(str(exc), hint=exc.hint)
        return
    if lockfile is None:
        if locked:
            fail(
                f"--locked: no lockfile at {short_path(lockfile_path(root), ctx)}",
                hint="run `rayspec lock` and commit the file (it pins the model of every agent)",
            )
        return
    drifts = check_locked(resolved, lockfile)
    if not drifts:
        return
    error_lines([d.message() for d in drifts], json_mode=json_mode, kind="lockfile drift")
    if not json_mode:
        console().print(
            "[dim]hint: run `rayspec lock` to re-pin, or restore the pinned model[/dim]"
        )
    raise typer.Exit(code=2)


def _load(target: str, ctx: Context) -> ResolvedWorkflow:
    try:
        return load_workflow(
            target, project_root=ctx.project_root, home=ctx.home, config=ctx.config
        )
    except RayspecError as exc:
        fail(str(exc), hint=exc.hint)
        raise AssertionError("unreachable") from None  # pragma: no cover


def _entries_json(entries: Mapping[str, LockEntry]) -> dict[str, Any]:
    return {key: entries[key].to_data() for key in sorted(entries)}


def _unpinnable(entries: Mapping[str, LockEntry]) -> list[str]:
    """Agents whose model is the provider's own default — recorded, but not really pinned."""
    return sorted(key for key, entry in entries.items() if entry.model is None)


def register(app: typer.Typer) -> None:
    @app.command()
    def lock(
        names: Annotated[
            list[str] | None,
            typer.Argument(help="Workflow names or paths (default: every discovered workflow)."),
        ] = None,
        check: Annotated[
            bool,
            typer.Option("--check", help="Report drift and exit 1; never write the lockfile."),
        ] = False,
        root: RootOption = None,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """Pin the literal model id and effort of every agent to .rayspec/rayspec.lock."""
        json_ = resolve_output(output, json_)
        ctx = make_context(root)
        targets = list(names or [])
        if not targets:
            targets = [r.name for r in discover_workflows(ctx.project_root, home=ctx.home)]
            if not targets:
                fail(
                    "no workflows found (nothing to lock)",
                    hint="run `rayspec init` to scaffold a project",
                )
                return
        unknown = [n for n in targets if workflow_label(n, ctx) is None]
        if unknown:
            fail(
                f"unknown workflow {unknown[0]!r}",
                hint="run `rayspec workflows` to list the discovered workflows",
            )
            return

        out = console()
        try:
            existing = load_lockfile(ctx.project_root)
        except LockfileError as exc:
            fail(str(exc), hint=exc.hint)
            return
        updates: dict[str, dict[str, LockEntry]] = {}
        drifts: list[str] = []
        unpinnable: list[str] = []
        for target in targets:
            resolved = _load(target, ctx)
            entries = lock_entries_for(resolved)
            updates[resolved.workflow.name] = entries
            unpinnable += [f"{resolved.workflow.name}: {k}" for k in _unpinnable(entries)]
            drifts += [
                f"{resolved.workflow.name}: {d.message()}" for d in check_locked(resolved, existing)
            ]

        if check:
            payload = {
                "path": str(lockfile_path(ctx.project_root)),
                "workflows": {n: _entries_json(e) for n, e in sorted(updates.items())},
                "drift": drifts,
                "checked": True,
            }
            if json_:
                print_json(payload)
            elif drifts:
                error_lines(drifts, kind="lockfile drift")
                out.print("[dim]hint: run `rayspec lock` to re-pin[/dim]")
            else:
                out.print(f"[green]lockfile is up to date[/green] ({len(updates)} workflow(s))")
            raise typer.Exit(code=1 if drifts else 0)

        merged = merged_workflows(existing, updates)
        path = write_lockfile(ctx.project_root, merged)
        agents = sum(len(entries) for entries in merged.values())
        if json_:
            print_json(
                {
                    "path": str(path),
                    "workflows": {n: _entries_json(e) for n, e in sorted(merged.items())},
                    "drift": [],
                    "checked": False,
                }
            )
            return
        out.print(f"wrote {short_path(path, ctx)} ({len(merged)} workflow(s), {agents} agent(s))")
        for entry in unpinnable:
            out.print(
                f"[yellow]note:[/yellow] {entry} has no literal model id — the provider's "
                "default applies and cannot be pinned",
                highlight=False,
            )


__all__ = ["LockedOption", "enforce_lockfile", "locked_enabled", "register"]
