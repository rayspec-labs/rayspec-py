# SPDX-License-Identifier: Apache-2.0
"""`rayspec validate [names...] [--json]` — load + validate workflows; exit 2 on errors."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.markup import escape

from rayspec.cli.commands import _loader_common as common
from rayspec.cli.commands._loader_common import (
    AllowUnsupportedOption,
    Context,
    JsonOption,
    OutputOption,
    RootOption,
    console,
    error_entries,
    error_problems,
    fail,
    make_context,
    message_problems,
    report_lines,
    resolve_output,
    workflow_label,
)
from rayspec.cli.commands.lock import LockedOption, locked_enabled
from rayspec.cli.commands.workflows import EMPTY_PROJECT_HINT
from rayspec.errors import RayspecError
from rayspec.limits import LockfileError, check_locked, load_lockfile
from rayspec.loader import discover_workflows, load_workflow, validate_workflow
from rayspec.loader.inputs import secret_input_names


def _validate_one(
    target: str, ctx: Context, *, allow_unsupported: bool, printer, lockfile: Any = None
) -> tuple[int, int, dict[str, Any]]:
    """Validate one workflow; returns ``(errors, warnings, json row)``."""
    caps = common.capability_source()
    try:
        rw = load_workflow(target, project_root=ctx.project_root, home=ctx.home, config=ctx.config)
    except RayspecError as exc:
        errors = error_entries(exc)
        printer(f"[bold]{escape(target)}[/bold]: [red]FAILED[/red] to load")
        report_lines("errors:", errors, style="red", printer=printer)
        if exc.hint:
            printer(f"  [dim]hint: {escape(exc.hint)}[/dim]")
        label = workflow_label(target, ctx)
        row = {
            "name": target,
            "path": label,
            "ok": False,
            "errors": errors,
            "warnings": [],
            # one object per problem, each with a non-null path (the document it sits in)
            "problems": error_problems(exc, path=label or target),
        }
        return len(errors), 0, row
    report = validate_workflow(
        rw,
        capabilities_for=caps.capabilities_for,
        template_checker=common.template_checker(),
        on_unsupported="warn" if allow_unsupported else "error",
        provider_ids=caps.provider_ids,
    )
    warnings = [*rw.warnings, *report.warnings]
    if caps.warning:
        warnings.append(caps.warning)
    # --locked: an agent that resolves differently than the lockfile pins it is an ERROR here,
    # not a warning — the point of the flag is that the run does not happen
    errors = [*report.errors, *(d.message() for d in check_locked(rw, lockfile))]
    status = "[green]OK[/green]" if not errors else "[red]FAILED[/red]"
    printer(f"[bold]{escape(rw.workflow.name)}[/bold] ({escape(rw.label)}): {status}")
    # the layers in force, named on every run: a guardrail nobody can see is one nobody trusts,
    # and policy is discovered against --root rather than against the workflow file
    printer(f"  [dim]{escape(report.policy_note)}[/dim]", soft_wrap=True)
    secrets = list(secret_input_names(rw.workflow))
    if secrets:
        # the (secret) marker — these values are never persisted and reach shell/python
        # steps through the environment only
        printer(
            f"  [dim]secret inputs: {escape(', '.join(secrets))} (secret; env-only, "
            "never persisted)[/dim]"
        )
    report_lines("errors:", errors, style="red", printer=printer)
    report_lines("warnings:", warnings, style="yellow", printer=printer)
    row = {
        "name": rw.workflow.name,
        "path": rw.label,
        "ok": not errors,
        "errors": errors,
        "warnings": list(warnings),
        "secret_inputs": secrets,
        "policy": {
            "layers": list(report.policy_layers),
            "searched": list(report.policy_searched),
        },
        "problems": message_problems(errors, path=rw.label),
    }
    return len(errors), len(warnings), row


def _unknown_names(names: list[str], ctx: Context) -> list[str]:
    """Targets that are neither a discovered workflow name nor a workflow file (the loader's
    rule, see :func:`workflow_label`)."""
    return [n for n in names if workflow_label(n, ctx) is None]


def register(app: typer.Typer) -> None:
    @app.command()
    def validate(  # noqa: PLR0917 - Typer options are positional by construction
        names: Annotated[
            list[str] | None,
            typer.Argument(help="Workflow names or paths (default: every discovered workflow)."),
        ] = None,
        root: RootOption = None,
        allow_unsupported: AllowUnsupportedOption = False,
        locked: LockedOption = None,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """Validate workflows (schema, graph, references, provider capabilities)."""
        json_ = resolve_output(output, json_)
        ctx = make_context(root)
        targets = list(names or [])
        if not targets:
            targets = [r.name for r in discover_workflows(ctx.project_root, home=ctx.home)]
            if not targets:
                if json_:
                    console().print("[]", markup=False, highlight=False)
                else:
                    out = console()
                    out.print("no workflows found (nothing to validate)")
                    out.print(f"[dim]hint: {EMPTY_PROJECT_HINT}[/dim]", highlight=False)
                return
        unknown = _unknown_names(targets, ctx)
        if unknown:
            fail(
                f"unknown workflow {unknown[0]!r}",
                hint="run `rayspec workflows` to list the discovered workflows",
            )
            return
        lockfile = None
        if locked_enabled(locked):
            try:
                lockfile = load_lockfile(ctx.project_root)
            except LockfileError as exc:
                fail(str(exc), hint=exc.hint)
                return
            if lockfile is None and locked:
                # the flag promises the models were pinned; the CI default does not, so a
                # project without a lockfile is simply not checked
                fail(
                    "--locked: no lockfile at .rayspec/rayspec.lock",
                    hint="run `rayspec lock` and commit the file",
                )
                return
        out = console()
        printer = (lambda *_, **__: None) if json_ else out.print
        total_errors = 0
        failed = 0
        rows: list[dict[str, Any]] = []
        for target in targets:
            errors, _, row = _validate_one(
                target,
                ctx,
                allow_unsupported=allow_unsupported,
                printer=printer,
                lockfile=lockfile,
            )
            rows.append(row)
            total_errors += errors
            failed += 1 if errors else 0
        if json_:
            out.print(json.dumps(rows, ensure_ascii=False), markup=False, highlight=False)
            if failed:
                raise typer.Exit(code=2)
            return
        summary = f"{len(targets)} workflow(s) validated"
        if failed:
            out.print(f"[red]{summary}, {failed} with errors ({total_errors} error(s))[/red]")
            raise typer.Exit(code=2)
        out.print(f"[green]{summary}, no errors[/green]")
