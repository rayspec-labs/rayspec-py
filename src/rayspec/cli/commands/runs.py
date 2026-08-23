# SPDX-License-Identifier: Apache-2.0
"""`rayspec runs` — the run sub-app: list runs (bare), record stub scripts, compare two runs.

``rayspec runs [--all] [--limit N] [--json]`` (no subcommand) lists runs newest first, exactly as
it did when ``runs`` was a flat command: default scope is the current project's store
(``$RAYSPEC_HOME/projects/<slug>``); ``--all`` lists every project under the home (adds a project
column). Outside a project (no ``.rayspec/`` and no git repository at or above the cwd /
``--root``) nothing is listed and no slug is minted: a one-line notice points at ``--all``.
The ``steps`` column is ``done/total`` with skipped steps counted as done and, for every run
that has not succeeded, the workflow's planned steps in the total. The ``started`` column is the
run's start as an age on a terminal and as an absolute UTC stamp on a redirected stream, so a
listing written to a file is a function of the store alone (see ``_runs_common``'s moments).

Module boundary: listing, formatting and argument plumbing only. The run lookup and the store
live in :mod:`rayspec.cli._runs_common`, the stub-script shape in
:mod:`rayspec.providers.stub`, the run comparison in :mod:`rayspec.cli.commands._runs_diff`.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.table import Table
from rich.text import Text

from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    console,
    err_console,
    fail,
    new_table,
    print_json,
    resolve_output,
)
from rayspec.errors import RayspecError
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord
from rayspec.textsafe import safe_text


def collect_runs(
    ctx: common.RunsContext, *, all_projects: bool, limit: int | None
) -> list[RunRecord]:
    """Runs newest first (``created_at``, then id) from the project store or every store."""
    if not all_projects:
        runs = ctx.store.list_runs(limit=None)
        runs.sort(key=_newest_first_key, reverse=True)
        return runs[:limit] if limit is not None else runs
    runs: list[RunRecord] = []
    for _slug, store in common.iter_project_stores(ctx.home):
        runs.extend(store.list_runs(limit=limit))
    runs.sort(key=_newest_first_key, reverse=True)
    return runs[:limit] if limit is not None else runs


def _newest_first_key(run: RunRecord) -> tuple[float, str]:
    """Sort key: ``created_at`` (runs created in the same second share an id prefix), then id."""
    created = run.created_at
    stamp = created.timestamp() if created is not None else 0.0
    return (stamp, run.run_id)


def is_project_dir(root: Path) -> bool:
    """Whether ``root`` is a rayspec project: it has a ``.rayspec/`` directory or is a git
    repository (``find_project_root`` only lands elsewhere when neither exists above the cwd)."""
    return (root / ".rayspec").is_dir() or (root / ".git").exists()


def runs_table(
    runs: list[RunRecord],
    *,
    show_project: bool,
    relative_ages: bool,
    planned: dict[str, set[str] | None] | None = None,
) -> Table:
    """The ``rayspec runs`` table (``planned`` = run id → planned step paths, see
    :func:`rayspec.cli._runs_common.planned_step_paths`).

    ``relative_ages`` decides what the ``started`` column says — ``3h ago`` for a reader watching
    a terminal, the absolute UTC stamp for a stream that will be diffed. It is a required
    argument, not a default: the caller knows who is reading, and the cell that ticks by itself
    is the one nobody chose. See :func:`rayspec.cli._runs_common.ages_are_relative`.
    """
    table = new_table()
    table.add_column("run")
    table.add_column("workflow")
    if show_project:
        table.add_column("project")
    table.add_column("status")
    table.add_column("started")
    table.add_column("duration", justify="right")
    table.add_column("steps", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("cost", justify="right")
    for run in runs:
        done, total = common.steps_progress(run, planned=(planned or {}).get(run.run_id))
        style = common.status_style(run.status.value)
        usage = run.total_usage()
        status = f"[{style}]{run.status.value}[/{style}]"
        if run.dry_run:
            status += " [dim](dry)[/dim]"
        # names/slugs come from run.json: plain text, never Rich markup, no escapes
        cells: list[Any] = [Text(safe_text(run.run_id)), Text(safe_text(run.workflow_name))]
        if show_project:
            cells.append(Text(safe_text(run.project_slug)))
        cells.extend(
            [
                status,
                common.fmt_when(run.started_at or run.created_at, relative=relative_ages),
                common.fmt_duration(common.run_duration_ms(run)),
                f"{done}/{total}",
                common.fmt_tokens(usage.total) if usage.total else "-",
                common.fmt_cost(run.total_cost_usd(), common.run_cost_source(run), usage),
            ]
        )
        table.add_row(*cells)
    return table


def list_runs(*, all_: bool, limit: int | None, root: Path | None, json_: bool) -> None:
    """The bare ``rayspec runs`` listing (kept byte-identical across the sub-app promotion)."""
    ctx = common.make_runs_context(root)
    out = console()
    if not all_ and not is_project_dir(ctx.project_root):
        # do not treat an arbitrary directory as a project (no slug, no empty store)
        err_console().print(
            f"not inside a rayspec project (no .rayspec/ or git repo at or above "
            f"{ctx.project_root}) — hint: rayspec runs --all",
            markup=False,
            highlight=False,
        )
        if json_:
            print_json([])
        return
    records = collect_runs(ctx, all_projects=all_, limit=limit)
    cache: dict[tuple[str, str], set[str] | None] = {}
    planned = {r.run_id: common.planned_step_paths(ctx, r, cache=cache) for r in records}
    if json_:
        print_json([common.run_row(r, planned=planned.get(r.run_id)) for r in records])
        return
    if not records:
        scope = "any project" if all_ else f"project {ctx.slug}"
        out.print(f"no runs for {scope} (run dir {ctx.store.root / 'runs'})", markup=False)
        return
    out.print(
        runs_table(
            records,
            show_project=all_,
            relative_ages=common.ages_are_relative(),
            planned=planned,
        )
    )


def stub_script_text(store: FileRunStore, run: RunRecord) -> str:
    """The recorded run as stub-script YAML (``runs stubs``), ready to write or print."""
    data = common.stub_script_data(store, run)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def write_script(output: Path, text: str) -> None:
    """Write a stub script to ``output`` atomically; any OS error is a usage error (exit 2).

    Temp file in the same directory + :func:`os.replace`, so a failure mid-write leaves the
    previous script intact instead of a truncated one — a stub script is a committed fixture.
    """
    import os
    import tempfile

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, output)
        tmp_path = None
    except OSError as exc:
        fail(
            f"cannot write {output}: {exc.strerror or exc}",
            hint="pass an -o path in an existing directory (or drop -o to print the script)",
        )
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def workflow_drift_warning(ctx: common.RunsContext, run: RunRecord) -> str | None:
    """``None`` unless the workflow's hash moved since the run — a recorded script may then key
    steps that no longer exist (or miss new ones). Any loader failure is silent: recording a run
    whose workflow was deleted is still useful."""
    try:
        resolved = common.load_resolved_for(ctx, run)
    except (RayspecError, OSError):
        return None
    if resolved.hash == run.workflow_hash:
        return None
    return (
        f"workflow {run.workflow_name!r} changed since run {run.run_id} "
        f"(hash {run.workflow_hash[:12]} → {resolved.hash[:12]}); recorded step keys may no "
        f"longer match"
    )


def group_root(ctx: typer.Context, root: Path | None) -> Path | None:
    """A subcommand's ``--root``, falling back to the one given before the subcommand name
    (``rayspec runs --root X diff a b``) — the group callback stashes it in ``ctx.obj``."""
    if root is not None:
        return root
    parent_root = getattr(ctx, "obj", None)
    return parent_root if isinstance(parent_root, Path) else None


def register(app: typer.Typer) -> None:
    runs_app = typer.Typer(
        name="runs",
        # no help= : the callback docstring is the group help, so `rayspec runs --help` keeps
        # leading with what the bare invocation does
        no_args_is_help=False,
        add_completion=False,
    )

    @runs_app.callback(invoke_without_command=True)
    def runs(  # noqa: PLR0917 - Typer options are positional by construction
        ctx: typer.Context,
        all_: Annotated[
            bool, typer.Option("--all", "-a", help="List runs of every project under RAYSPEC_HOME.")
        ] = False,
        limit: Annotated[
            int | None, typer.Option("--limit", "-n", help="Show at most N runs.", min=1)
        ] = None,
        json_: JsonOption = False,
        output: OutputOption = None,
        root: RootOption = None,
    ) -> None:
        """List runs (newest first) of the current project, or of every project with --all."""
        json_ = resolve_output(output, json_)
        ctx.obj = root
        if ctx.invoked_subcommand is not None:
            # only --root is forwarded (ctx.obj); the listing flags would be silently dropped
            given = [
                name
                for name, used in (
                    ("--json", json_ and output is None),
                    ("--output", output is not None),
                    ("--all", all_),
                    ("--limit", limit is not None),
                )
                if used
            ]
            if given:
                fail(
                    f"{', '.join(given)} belongs to the `rayspec runs` listing, not to "
                    f"`{ctx.invoked_subcommand}`",
                    hint=f"put it after the subcommand: rayspec runs {ctx.invoked_subcommand} "
                    f"... {given[0]}",
                )
            return
        list_runs(all_=all_, limit=limit, root=root, json_=json_)

    @runs_app.command("stubs")
    def stubs(  # noqa: PLR0917 - Typer options are positional by construction
        ctx: typer.Context,
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        output: Annotated[
            Path | None,
            typer.Option("--output", "-o", help="Write the script here instead of stdout."),
        ] = None,
        redact: Annotated[
            bool,
            typer.Option(
                "--redact",
                help="Refused: a recording is never given secret values to redact (exits 2).",
            ),
        ] = False,
        force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
        root: RootOption = None,
    ) -> None:
        """Write a stub script from a stored run (replay it with `run --dry-run --stubs`)."""
        if redact:
            # A settled decision, not a gap waiting for plumbing. `rayspec.redact.Redactor`
            # ships and every writer already goes through it; what `--redact` asks for is
            # something else — may a RECORDING command obtain secret values? A redactor
            # replaces only values it is given, and a run's are never persisted, so `stubs`
            # would have to demand them the way `resume` does, in order to write a file whose
            # whole purpose is to be committed. Exact-match redaction cannot promise that a
            # value a step transformed is gone, so the flag would advertise a safety it cannot
            # keep. It therefore always refuses — never a silent no-op on a run that happens
            # to have no secrets — and a run that HAS secret inputs is refused just below.
            fail(
                "--redact is refused, not unimplemented: a recording is never given secret "
                "values. rayspec would have to ask you for them, the way `resume` does, to "
                "write a file you are meant to commit — and exact-match redaction cannot "
                "promise that a value a step transformed is gone",
                hint="record a run that has no secret inputs; what such a run stored was "
                "already redacted when it was written",
            )
        rc = common.make_runs_context(group_root(ctx, root))
        store, record = common.lookup_run(rc, run)
        refusal = common.secret_refusal(record)
        if refusal is not None:
            fail(
                refusal,
                hint="re-run the workflow without the secret inputs to record it",
            )
        if output is not None and output.exists() and not force:
            fail(f"{output} already exists", hint="pass --force to overwrite it")
        drift = workflow_drift_warning(rc, record)
        if drift is not None:
            err_console().print(drift, markup=False, highlight=False)
        for note in common.recording_notes(record):
            err_console().print(note, markup=False, highlight=False)
        text = stub_script_text(store, record)
        out = console()
        if output is None:
            out.print(text, markup=False, highlight=False, end="")
            return
        write_script(output, text)
        out.print(f"wrote stub script for run {record.run_id} to {output}", markup=False)

    @runs_app.command("diff")
    def diff(  # noqa: PLR0917 - Typer options are positional by construction
        ctx: typer.Context,
        run_a: Annotated[str, typer.Argument(metavar="A", help="First run id or prefix.")],
        run_b: Annotated[str, typer.Argument(metavar="B", help="Second run id or prefix.")],
        json_: JsonOption = False,
        output: OutputOption = None,
        exit_code: Annotated[
            bool, typer.Option("--exit-code", help="Exit 1 when anything differs (CI gate).")
        ] = False,
        outputs: Annotated[
            bool, typer.Option("--outputs", help="Also print a unified diff of step outputs.")
        ] = False,
        steps: Annotated[bool, typer.Option("--steps", help="List unchanged steps too.")] = False,
        across_projects: Annotated[
            bool,
            typer.Option(
                "--across-projects", help="Allow two runs from different projects (rare)."
            ),
        ] = False,
        root: RootOption = None,
    ) -> None:
        """Compare two runs of one workflow: status, timing, cost, steps and outputs."""
        json_ = resolve_output(output, json_)
        from rayspec.cli.commands import _runs_diff

        rc = common.make_runs_context(group_root(ctx, root))
        store_a, record_a = common.lookup_run(rc, run_a)
        store_b, record_b = common.lookup_run(rc, run_b)
        if record_a.workflow_name != record_b.workflow_name:
            # never guess an alignment between two different graphs
            fail(
                f"runs of two different workflows: {record_a.run_id} ran "
                f"{record_a.workflow_name!r}, {record_b.run_id} ran {record_b.workflow_name!r}",
                hint="`rayspec runs diff` compares two runs of the SAME workflow",
            )
        if record_a.project_slug != record_b.project_slug and not across_projects:
            # a run id prefix resolves home-wide, so two same-named workflows of two
            # unrelated repos compare "cleanly" and every step looks like drift
            fail(
                f"runs of two different projects: {record_a.run_id} ran in "
                f"{record_a.project_slug!r}, {record_b.run_id} ran in {record_b.project_slug!r}",
                hint="pass --across-projects if you really mean to compare them",
            )
        result = _runs_diff.build_diff(store_a, record_a, store_b, record_b)
        out = console()
        if json_:
            print_json(result.to_json())
        else:
            if outputs:
                for record in (record_a, record_b):
                    notice = common.secret_output_notice(record)
                    if notice is not None:
                        err_console().print(notice, markup=False, highlight=False)
            _runs_diff.render(result, out, show_steps=steps, show_outputs=outputs)
        if exit_code and result.changed:
            raise typer.Exit(code=1)

    app.add_typer(runs_app, name="runs")


__all__ = [
    "collect_runs",
    "group_root",
    "is_project_dir",
    "list_runs",
    "register",
    "runs_table",
    "stub_script_text",
    "workflow_drift_warning",
    "write_script",
]
