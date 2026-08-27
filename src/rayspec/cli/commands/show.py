# SPDX-License-Identifier: Apache-2.0
"""`rayspec show <run|prefix> [--json]` — one run in detail.

Header (id, workflow, status/reason, project, timings, resume count, pid/host while running,
``steps done/total (n ok · m skipped)``, tokens, cost with the run-level marker and a
``(n steps unpriced)`` note for partial costs), workspace block, per-step table (path, kind,
status, attempts, duration, cost/tokens, output preview), workflow outputs, a ``warnings:``
block (provider warnings from the step streams + engine ``warning`` events) and the pause
block (step, message, token, how to approve/reject). Thin command over
:mod:`rayspec.cli._runs_common`. Everything that comes out of ``run.json``, an output file or a
stream is untrusted text: rendered as plain :class:`rich.text.Text` with escape sequences
removed (:mod:`rayspec.textsafe`), never parsed as Rich markup.

A run that can still be resumed also gets a ``secret inputs to re-supply:`` line naming
the ``secret: true`` inputs whose values were never persisted, so the answer to "what does this
paused run need from me?" is in the same place as the approve/reject hint.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    console,
    new_table,
    print_json,
    resolve_output,
)
from rayspec.cli.commands.run import pause_actions
from rayspec.loader.inputs import SECRET_PLACEHOLDER, env_var_name
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord
from rayspec.textsafe import safe_text

_log = logging.getLogger(__name__)


def collect_warnings(store: FileRunStore, run: RunRecord) -> list[str]:
    """``<step>: <message>`` for every ``warning`` event of the run and every ``warning`` stream
    record of its recorded steps (events first, then steps in record order); plain safe text."""
    out: list[str] = []
    for event in store.read_events(run.run_id):
        if event.type.value != "warning":
            continue
        message = safe_text(event.data.get("message") or event.data.get("warning") or "")
        message = " ".join(message.split())
        if not message:
            continue
        out.append(f"{safe_text(event.step_path)}: {message}" if event.step_path else message)
    for path in run.steps:
        try:
            # kinds= pre-filters the lines (no per-delta parsing of a multi-MB transcript); any
            # failure reading one stream (torn/foreign records, permissions) loses only that
            # stream's warnings — ``show`` is the first thing a user runs on a broken run
            records = list(store.read_stream(run.run_id, path, kinds={"warning"}))
        except Exception as exc:  # best effort, see above
            _log.debug("warnings of step %s unreadable: %s", path, exc)
            continue
        for rec in records:
            text = " ".join(safe_text(rec.text).split())
            if text:
                out.append(f"{safe_text(path)}: {text}")
    return out


def show_payload(
    store: FileRunStore, run: RunRecord, *, planned: set[str] | None = None
) -> dict[str, Any]:
    """The ``rayspec show --json`` object: the run row + ``run_dir``, ``inputs``, ``outputs``,
    ``steps`` (records with previews), ``warnings`` and the raw ``record``."""
    payload = common.run_row(run, planned=planned)
    payload["run_dir"] = str(store.run_dir(run.run_id))
    payload["inputs"] = run.inputs
    payload["outputs"] = run.outputs
    payload["workflow_path"] = run.workflow_path
    payload["workflow_hash"] = run.workflow_hash
    payload["project_root"] = run.project_root
    payload["steps"] = [common.step_row(store, run, rec) for rec in run.steps.values()]
    payload["artifacts"] = artifact_rows(run)
    payload["warnings"] = collect_warnings(store, run)
    payload["pending_secret_inputs"] = list(pending_secret_inputs(run))
    payload["record"] = json.loads(run.model_dump_json())
    return payload


def artifact_rows(run: RunRecord) -> list[dict[str, Any]]:
    """One row per file a step declared under ``artifacts:`` and delivered, in record order.

    ``ref`` is where the run directory keeps its copy (``None`` when no copy was kept) and
    ``sha256``/``size`` describe the stored (redacted) bytes; the content of an artifact is
    never part of a record, so there is nothing else to report.
    """
    return [
        {
            "step": path,
            "path": artifact.path,
            "ref": artifact.ref,
            "sha256": artifact.sha256,
            "size": artifact.size,
        }
        for path, rec in run.steps.items()
        for artifact in rec.artifacts
    ]


def artifacts_table(run: RunRecord) -> Table:
    """The ``artifacts`` block: which step promised which file, and what was stored."""
    table = new_table(title="artifacts")
    table.add_column("step")
    table.add_column("file")
    table.add_column("size", justify="right")
    table.add_column("sha256")
    table.add_column("stored")
    for row in artifact_rows(run):
        table.add_row(
            Text(_cell(row["step"])),
            Text(_cell(row["path"])),
            fmt_size(row["size"]),
            Text(_cell(row["sha256"])[:12]),
            Text(_cell(row["ref"] or "-")),
        )
    return table


def fmt_size(size: Any) -> str:
    """``0 B`` · ``512 B`` · ``1.4 KB`` · ``12.0 MB`` — never a raw byte count for a big file."""
    if not isinstance(size, int) or size < 0:
        return "-"
    if size < 1024:
        return f"{size} B"
    for unit, scale in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3)):
        if size < scale * 1024 or unit == "GB":
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"  # pragma: no cover - the loop always returns


def pending_secret_inputs(run: RunRecord) -> tuple[str, ...]:
    """The ``secret: true`` inputs a resume entry would have to supply again.

    A secret is never persisted: ``run.inputs`` holds :data:`SECRET_PLACEHOLDER` for the ones
    that *were* given, and those are exactly the ones ``resume``/``approve``/``reject`` need
    back. Empty once the run has reached a final status that cannot be resumed.
    """
    if run.status.value in {"succeeded", "cancelled"}:
        return ()
    return tuple(name for name in run.secret_inputs if run.inputs.get(name) == SECRET_PLACEHOLDER)


def print_secret_inputs(out: Console, run: RunRecord, *, configured: Collection[str] = ()) -> None:
    """The ``secret inputs to re-supply:`` block; nothing when there are none.

    ``configured`` are the names ``config.secrets`` supplies by itself: rayspec re-fetches them
    on ``resume``/``approve``/``reject``, so asking the user for them would be asking for work
    the feature exists to remove. They are listed separately, and when every
    pending secret is configured there is nothing to ask for at all.
    """
    names = pending_secret_inputs(run)
    if not names:
        return
    auto = tuple(n for n in names if n in configured)
    manual = tuple(n for n in names if n not in configured)
    out.print("")
    if auto:
        out.print(
            Text.assemble(("supplied by config.secrets", "green"), f": {', '.join(auto)}"),
        )
    if not manual:
        return
    out.print(Text.assemble(("secret inputs to re-supply", "yellow"), f": {', '.join(manual)}"))
    out.print(
        "  "
        + "  ".join(f"--input {n}=…" for n in manual)
        + "  ·  or "
        + ", ".join(env_var_name(n) for n in manual)
        + "  ·  or a `secrets:` entry in config.yaml (re-fetched automatically)",
        markup=False,
        highlight=False,
    )


def steps_table(store: FileRunStore, run: RunRecord) -> Table:
    """Per-step table in record order (nested paths appear as stored)."""
    table = new_table(title="steps")
    table.add_column("step")
    table.add_column("kind")
    table.add_column("status")
    table.add_column("att", justify="right")
    table.add_column("duration", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("output")
    for rec in run.steps.values():
        style = common.status_style(rec.status.value)
        status = f"[{style}]{rec.status.value}[/{style}]"
        if rec.tolerated:
            status += " [dim](tolerated)[/dim]"
        detail = ""
        if rec.error is not None:
            detail = f"{rec.error.type}: {rec.error.message}"
        elif rec.skip_reason:
            detail = rec.skip_reason
        elif rec.status.value == "paused":
            detail = "awaiting approval"
        else:
            detail = common.output_preview(store, run, rec)
        # run data (paths, error messages, output previews) is untrusted text: never markup,
        # no escape sequences; one row per step
        table.add_row(
            Text(_cell(rec.path)),
            Text(_cell(rec.kind)),
            status,
            str(rec.attempts),
            common.fmt_duration(rec.duration_ms),
            common.fmt_tokens(rec.usage.total) if rec.usage.total else "-",
            common.fmt_cost(rec.cost_usd, rec.cost_source, rec.usage),
            Text(_cell(detail)),
        )
    return table


def _cell(value: Any) -> str:
    """One-line safe text for a table cell / header field."""
    return safe_text(value, keep_newlines=False)


def toolchain_lines(run: RunRecord) -> list[str]:
    """The ``toolchain:`` block of ``show``: what produced this run.

    Empty for records written before the field existed. Everything is run data, so callers must
    render the lines as plain text.
    """
    toolchain = run.toolchain
    if not isinstance(toolchain, dict):
        return []
    head = "  ".join(
        f"{label} {toolchain[key]}"
        for label, key in (("rayspec", "rayspec"), ("python", "python"), ("on", "platform"))
        if toolchain.get(key)
    )
    out = [head] if head else []
    providers = toolchain.get("providers")
    if isinstance(providers, dict):
        for name, info in providers.items():
            if not isinstance(info, dict):
                continue
            if info.get("error"):
                out.append(f"{name}: unavailable ({info['error']})")
                continue
            parts = [
                f"{label} {info[key]}"
                for label, key in (("sdk", "sdk_version"), ("cli", "cli_version"))
                if info.get(key)
            ]
            path = f"  ({info['cli_path']})" if info.get("cli_path") else ""
            out.append(f"{name}: {'  '.join(parts) or 'no version reported'}{path}")
    models = toolchain.get("models")
    if isinstance(models, dict):
        for agent, model in models.items():
            out.append(f"{agent} → {model or 'provider default'}")
    return out


def print_show(
    out: Console,
    store: FileRunStore,
    run: RunRecord,
    *,
    planned: set[str] | None = None,
    configured_secrets: Collection[str] = (),
) -> None:
    """Render the human-readable ``rayspec show`` view (``planned`` = the workflow's planned
    step paths for an unfinished run, see ``planned_step_paths``)."""
    style = common.status_style(run.status.value)
    dry = "  [dim](dry run — stub providers, no model calls)[/dim]" if run.dry_run else ""
    # the recorded blast radius belongs beside the recorded rehearsal flag: both say what the
    # next resume of this run will do, and neither is worth reading run.json by hand for
    fast = "  [dim](fail-fast — a failure cancels running siblings)[/dim]" if run.fail_fast else ""
    run_id = _cell(run.run_id)
    out.print(f"[bold]run {run_id}[/bold]  [{style}]{run.status.value}[/{style}]{dry}{fast}")
    if run.reason:
        out.print(f"  reason:     {_cell(run.reason)}", markup=False)
    out.print(
        f"  workflow:   {_cell(run.workflow_name)} ({_cell(run.workflow_path)})", markup=False
    )
    out.print(f"  project:    {_cell(run.project_slug)} ({_cell(run.project_root)})", markup=False)
    if run.inputs:
        inputs = json.dumps(run.inputs, ensure_ascii=False)
        out.print(f"  inputs:     {_cell(inputs)}", markup=False)
    # the absolute stamp is the answer; the age beside it is the terminal's convenience, so it
    # is printed only where somebody is watching and only as an AGE — `fmt_age` never degrades
    # into a second, shorter copy of the stamp it stands next to, and a run stamped in the future
    # reads `in 9d` instead of `0s ago`
    moment = run.started_at or run.created_at
    started = common.fmt_stamp(moment)
    if moment is not None and common.ages_are_relative():
        started += f" ({common.fmt_age(moment)})"
    out.print(f"  started:    {started}", markup=False)
    if run.ended_at is not None:
        out.print(f"  ended:      {common.fmt_stamp(run.ended_at)}", markup=False)
    out.print(f"  duration:   {common.fmt_duration(common.run_duration_ms(run))}", markup=False)
    done, total = common.steps_progress(run, planned=planned)
    usage = run.total_usage()
    source = common.run_cost_source(run)
    cost = common.fmt_cost(run.total_cost_usd(), source, usage)
    line = Text(
        f"  steps:      {done}/{total} done ({common.steps_detail(run)})   "
        f"tokens: {common.fmt_tokens(usage.total)}   cost: {cost}"
    )
    if source == "partial":
        unpriced = common.unpriced_steps(run)
        line.append(f" ({unpriced} step{'s' if unpriced != 1 else ''} unpriced)", style="dim")
    out.print(line)
    if run.resume_count:
        out.print(f"  resumed:    {run.resume_count}x", markup=False)
    if run.status.value in {"running", "paused"} and run.pid:
        # a paused run's process has exited (exit 3); say so instead of looking live
        live = " (alive)" if common.pid_alive(run) else " (exited)"
        out.print(f"  pid:        {run.pid} on {run.host or '?'}{live}", markup=False)
    out.print(f"  run dir:    {store.run_dir(run.run_id)}", markup=False)
    ws = run.workspace
    out.print(f"  workspace:  {_cell(ws.isolation)}  {_cell(ws.workdir or '')}", markup=False)
    if ws.branch or ws.base_branch or ws.head_sha:
        base = f" from {_cell(ws.base_branch)}" if ws.base_branch else ""
        base_sha = f" @ {_cell(ws.base_sha[:12])}" if ws.base_sha else ""
        head = f"  head {_cell(ws.head_sha[:12])}" if ws.head_sha else ""
        branch = _cell(ws.branch or "-")
        out.print(f"              branch {branch}{base}{base_sha}{head}", markup=False)
    lines = toolchain_lines(run)
    if lines:
        out.print("  toolchain:  " + _cell(lines[0]), markup=False)
        for line in lines[1:]:
            out.print(f"              {_cell(line)}", markup=False)
    out.print("")
    if run.steps:
        out.print(steps_table(store, run))
    else:
        out.print("  (no steps recorded yet)")
    if any(rec.artifacts for rec in run.steps.values()):
        out.print("")
        out.print(artifacts_table(run))
    warnings = collect_warnings(store, run)
    if warnings:
        out.print("")
        out.print("[bold]warnings:[/bold]")
        for warning in warnings:
            out.print(Text.assemble(("  ! ", "yellow"), warning))
    if run.outputs:
        out.print("")
        table = new_table(title="outputs")
        table.add_column("name")
        table.add_column("value")
        for name, value in run.outputs.items():
            table.add_row(Text(safe_text(name)), Text(common.value_text(value)))
        out.print(table)
    if run.pause is not None:
        out.print("")
        pause = run.pause
        label = "paused" if run.status.value == "paused" else "last gate"
        out.print(
            Text.assemble(
                (label, "yellow"),
                f" at {_cell(pause.step)} (token {_cell(pause.token)}, "
                f"{common.fmt_when(pause.requested_at, relative=common.ages_are_relative())})",
            )
        )
        out.print(f"  {safe_text(pause.message)}", markup=False)
        if pause.decision is not None:
            word = "approved" if pause.decision.approved else "rejected"
            comment = f" — {_cell(pause.decision.comment)}" if pause.decision.comment else ""
            out.print(
                f"  decision recorded: {word} by {_cell(pause.decision.by)}{comment} "
                "(resume to apply it)",
                markup=False,
            )
        elif run.status.value == "paused":
            out.print("  " + pause_actions(run.run_id, pause.reason), markup=False)
    # what a resume entry still needs from the user — minus what config.secrets re-fetches
    print_secret_inputs(out, run, configured=configured_secrets)


def register(app: typer.Typer) -> None:
    @app.command()
    def show(
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        json_: JsonOption = False,
        output: OutputOption = None,
        root: RootOption = None,
    ) -> None:
        """Show one run: header, workspace, steps, outputs and pause state."""
        json_ = resolve_output(output, json_)
        ctx = common.make_runs_context(root)
        store, record = common.lookup_run(ctx, run)
        record = common.reconcile_run(store, record)
        out = console()
        planned = common.planned_step_paths(ctx, record)
        if json_:
            print_json(show_payload(store, record, planned=planned))
            return
        print_show(
            out, store, record, planned=planned, configured_secrets=tuple(ctx.config.secrets)
        )


__all__ = [
    "artifact_rows",
    "artifacts_table",
    "collect_warnings",
    "fmt_size",
    "pending_secret_inputs",
    "print_secret_inputs",
    "print_show",
    "register",
    "show_payload",
    "steps_table",
    "toolchain_lines",
]
