# SPDX-License-Identifier: Apache-2.0
"""`rayspec test [<workflow>]` — run a project's declarative workflow cases offline.

Thin command: discovery, filtering and execution live in :mod:`rayspec.testing`; this module only
resolves the project context, streams one line per case and renders the report. Every case is a
dry run against the stub provider, so the command needs no credentials and no network — the
project's ``.rayspec/.env`` is deliberately **not** loaded (``project_env=False``): a case that
needs a variable pins it in its own ``env:``.

By default no subprocess is started either. ``--exec-shell`` is the operator's authorisation to
run ``shell:``/``python:`` steps for real; a case file's ``exec_shell: true`` is only a
declaration that the case wants it, and is refused (exit 2, naming its ``file:line``) unless the
flag is given. A checked-in YAML file must never be able to widen what this command does — this is
the command a reviewer or a CI job runs against an untrusted checkout.

Exit codes: ``0`` every case passed · ``1`` at least one case failed · ``2`` usage (a filter that
matches nothing, no cases at all, a case that needs ``--exec-shell``) or a malformed case file.
``--junit`` writes its file in every one of those cases.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer
from rich.text import Text

from rayspec.cli._docs import docs_url
from rayspec.cli.commands import _loader_common as common
from rayspec.cli.commands._loader_common import (
    OutputOption,
    RootOption,
    make_context,
    resolve_output,
)
from rayspec.cli.commands.run import approval_classes_for
from rayspec.testing import discover_suites, run_case
from rayspec.testing.report import (
    CaseResult,
    json_line,
    junit_error_xml,
    junit_xml,
    results_json,
    summary_line,
)
from rayspec.testing.spec import Case, CaseFileError, Suite

EXIT_FAILED = 1
EXIT_USAGE = 2

#: What to tell a project that has no cases at all. Both placements are ones the project itself
#: can take: ``examples/<name>/checks.yaml`` is this repository's layout, and a project scaffolded
#: from an example (or installed from a wheel) has no ``examples/`` directory to put a file in.
NO_CASES_HINT = (
    "put cases in .rayspec/tests/<workflow>/<case>.yaml, or a checks.yaml at the project root: "
    + docs_url("docs/testing.md")
)


def selected(
    suites: list[Suite],
    *,
    workflow: str | None,
    case_id: str | None,
    pattern: str | None,
) -> list[tuple[Suite, Case]]:
    """The ``(suite, case)`` pairs a filter combination selects, in discovery order.

    ``workflow`` matches the case's ``workflow:`` or its suite name, ``case_id`` the case id and
    ``pattern`` any substring of ``<suite>:<case>``; the filters combine with AND.
    """
    pairs = [(suite, case) for suite in suites for case in suite.checks]
    if workflow is not None:
        pairs = [(s, c) for s, c in pairs if workflow in {c.workflow, s.name}]
    if case_id is not None:
        pairs = [(s, c) for s, c in pairs if c.id == case_id]
    if pattern is not None:
        pairs = [(s, c) for s, c in pairs if pattern in f"{s.name}:{c.id}"]
    return pairs


def needs_authorisation(pairs: list[tuple[Suite, Case]]) -> tuple[Suite, Case] | None:
    """The first selected case that declares ``exec_shell: true``, if any.

    Data may not widen what the command does: without ``--exec-shell`` such a case is refused
    rather than silently executed, because the plain command promises no subprocess.
    """
    return next(((s, c) for s, c in pairs if c.exec_shell), None)


def _known(suites: list[Suite]) -> str:
    names = [f"{suite.name}:{case.id}" for suite in suites for case in suite.checks]
    shown = ", ".join(names[:12])
    return shown + (f", … ({len(names)} total)" if len(names) > 12 else "")


def register(app: typer.Typer) -> None:
    @app.command()
    def test(  # noqa: PLR0917 - Typer options are positional by construction
        workflow: Annotated[
            str | None,
            typer.Argument(help="Only cases of this workflow (or suite).", show_default=False),
        ] = None,
        case: Annotated[
            str | None, typer.Option("--case", help="Only this case id.", show_default=False)
        ] = None,
        pattern: Annotated[
            str | None,
            typer.Option(
                "-k", "--select", help="Only cases whose <suite>:<case> contains this substring."
            ),
        ] = None,
        junit: Annotated[
            Path | None,
            typer.Option("--junit", help="Write a JUnit XML report to this file."),
        ] = None,
        json_: Annotated[
            bool, typer.Option("--json", help="One JSON object with every case's outcome.")
        ] = False,
        output: OutputOption = None,
        exec_shell: Annotated[
            bool, typer.Option("--exec-shell", help="Run shell/python steps in every case.")
        ] = False,
        root: RootOption = None,
    ) -> None:
        """Run the project's workflow test cases (dry run, stub provider)."""
        json_ = resolve_output(output, json_)
        # a case is a dry run against the stub provider: it needs no credentials, so the
        # project's .rayspec/.env is not applied to this process
        ctx = make_context(root, project_env=False)
        out = common.err_console() if json_ else common.console()

        def usage_exit(message: str, *, detail: str = "", hint: str = "") -> None:
            """Report a usage error, writing the promised ``--junit`` document first."""
            if junit is not None:
                junit.parent.mkdir(parents=True, exist_ok=True)
                junit.write_text(junit_error_xml(message, detail=detail), encoding="utf-8")
            common.err_console().print(Text(f"error: {message}"), highlight=False)
            for line in detail.splitlines():
                common.err_console().print(Text(f"  {line}"), highlight=False)
            if hint:
                common.err_console().print(Text(f"hint: {hint}", style="dim"))
            raise typer.Exit(code=EXIT_USAGE)

        try:
            suites = discover_suites(ctx.project_root)
        except CaseFileError as exc:
            usage_exit(
                exc.errors[0],
                detail="\n".join(exc.errors[1:]),
                hint=exc.hint or "",
            )
            return
        pairs = selected(suites, workflow=workflow, case_id=case, pattern=pattern)
        if not pairs:
            known = _known(suites)
            filters = ", ".join(
                part
                for part in (
                    f"workflow {workflow!r}" if workflow else "",
                    f"--case {case!r}" if case else "",
                    f"-k {pattern!r}" if pattern else "",
                )
                if part
            )
            usage_exit(
                f"no test case matches {filters}" if known else "no test cases found",
                hint=f"known cases: {known}" if known else NO_CASES_HINT,
            )
            return
        if not exec_shell:
            declared = needs_authorisation(pairs)
            if declared is not None:
                suite, spec = declared
                usage_exit(
                    f"case {suite.name}:{spec.id} asks for `exec_shell: true`",
                    detail=(
                        "`rayspec test` runs no subprocess unless you say so — a case file may "
                        "not widen that\n"
                        f"at {suite.location(spec.id).of('exec_shell')}"
                    ),
                    hint="pass --exec-shell to authorise it, or drop the key from the case",
                )
                return
        started = time.monotonic()
        results: list[CaseResult] = []
        for suite, case_spec in pairs:
            result = run_case(
                suite,
                case_spec,
                home=ctx.home,
                exec_shell=exec_shell,
                keep_run_dir=False,
                # the operator's rules, not the case file's: `--exec-shell` runs a gated body
                # for real, so a class held shut holds here too
                approval_classes=approval_classes_for(suite.root, ctx.home),
            )
            results.append(result)
            if not json_:
                mark = "[green]ok[/green]" if result.ok else "[red]FAILED[/red]"
                out.print(f"{mark} {result.name} [dim]({result.duration_s:.2f}s)[/dim]")
        elapsed = time.monotonic() - started
        if junit is not None:
            junit.parent.mkdir(parents=True, exist_ok=True)
            junit.write_text(junit_xml(results, elapsed_s=elapsed), encoding="utf-8")
        failed = [r for r in results if not r.ok]
        if json_:
            common.console().print(
                json_line(results_json(results, elapsed_s=elapsed)), markup=False, highlight=False
            )
        else:
            for result in failed:
                out.print("")
                out.print(Text(result.name, style="bold red"), highlight=False)
                for failure in result.failures:
                    for line in failure.lines():
                        out.print(Text("  " + line), highlight=False)
                if result.run_id:
                    out.print(
                        Text(f"  run {result.run_id} · {result.run_dir}", style="dim"),
                        highlight=False,
                    )
            if junit is not None:
                out.print(f"[dim]junit: {junit}[/dim]", highlight=False)
            style = "red" if failed else "green"
            out.print(f"[{style}]{summary_line(results, elapsed)}[/{style}]")
        if failed:
            raise typer.Exit(code=EXIT_FAILED)


__all__ = ["EXIT_FAILED", "EXIT_USAGE", "NO_CASES_HINT", "register", "selected"]
