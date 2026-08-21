# SPDX-License-Identifier: Apache-2.0
"""`rayspec audit <run> [--commands] [--json]` — what that run actually did, as a ledger.

One line per fact the run left behind, in time order: the run itself (started, workspace,
paused, finished), every step, every command an agent ran, every tool it called, every file it
reported changing, every warning, and every approval with the identity behind it. It is the
question ``logs --stream`` answers only if you read the whole transcript with a careful eye.

Module boundary: **read-only**. This command opens ``run.json``, ``events.jsonl`` and the step
``stream.jsonl`` files through the store and prints them; it never writes, never re-runs
anything and never contacts anything. The row shape is the store's
(:func:`rayspec.store.file.audit_entry_for_event` / ``audit_entry_for_stream``), which is also
what an enabled ``audit.jsonl`` holds — so a rendered ledger and a stored one always agree.

Honest limits: this is a report over the files of ONE run on THIS machine. It proves nothing
about them — anybody who can read the run directory can also edit it — and it has no notion of
other runs, other projects or other people.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
    resolve_output,
)
from rayspec.store.file import (
    FileRunStore,
    audit_entry_for_event,
    audit_entry_for_stream,
    finish_audit_row,
)
from rayspec.store.model import RunRecord
from rayspec.textsafe import safe_text

#: Row kinds and the colour each gets in the table.
ROW_STYLES: dict[str, str] = {
    "run": "bold",
    "step": "cyan",
    "command": "bold yellow",
    "tool": "magenta",
    "file": "green",
    "approval": "bold blue",
    "warning": "yellow",
}

#: Step kinds that ARE a command: with ``--commands`` their rows are kept next to the commands
#: an agent ran, because a ``shell:``/``python:`` step is the run executing something itself.
COMMAND_STEP_KINDS = frozenset({"shell", "python"})


def is_command_row(row: dict[str, Any]) -> bool:
    """Whether ``row`` describes something the run executed (``--commands``).

    That is a ``command`` row — a command an agent ran, whether the adapter reported it as a
    ``command_start`` or as a tool call carrying a command line — or the row of a ``shell:``/
    ``python:`` step, which is rayspec running a command itself. A step row names the step and
    its kind, not the body: the rendered body is not kept in the run directory (``rayspec
    explain`` re-renders it from the workflow).
    """
    if row["kind"] == "command":
        return True
    return row["kind"] == "step" and row["data"].get("kind") in COMMAND_STEP_KINDS


def collect_rows(store: FileRunStore, run: RunRecord) -> list[dict[str, Any]]:
    """Every ledger row of one run, oldest first.

    Lifecycle events come from ``events.jsonl`` and the per-step records from each recorded
    step's ``stream.jsonl``; both are mapped through the store's own row derivation, so this
    renders exactly what an enabled ``audit.jsonl`` would have stored. Rows are sorted by
    timestamp, ties keeping the order they were read in (events before streams).
    """
    rows: list[dict[str, Any]] = []
    for event in store.read_events(run.run_id):
        entry = audit_entry_for_event(event)
        if entry is not None:
            rows.append(finish_audit_row(entry))
    for path in run.steps:
        try:
            records = list(store.read_stream(run.run_id, path))
        except (OSError, ValueError):
            continue
        for record in records:
            entry = audit_entry_for_stream(path, record)
            if entry is not None:
                rows.append(finish_audit_row(entry))
    rows.sort(key=_row_time)
    return rows


def _row_time(row: dict[str, Any]) -> datetime:
    """Parse a row's ``ts`` for sorting; an unreadable stamp sorts first, never raises."""
    try:
        moment = datetime.fromisoformat(str(row["ts"]))
    except (KeyError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def actor_line(run: RunRecord) -> str:
    """``launcher@example.com (git, ci: github-actions)`` — who ran it, ``—`` when unrecorded."""
    actor = run.actor
    if actor is None:
        return "— (not recorded)"
    extras = [actor.source]
    if actor.ci:
        extras.append(f"ci: {actor.ci}")
    for provider, account in sorted(actor.provider_accounts.items()):
        extras.append(f"{provider}: {account}")
    return f"{safe_text(actor.id, keep_newlines=False)} ({', '.join(safe_text(e) for e in extras)})"


def audit_payload(store: FileRunStore, run: RunRecord, *, commands: bool) -> dict[str, Any]:
    """The ``--json`` object: the run's identity, whether it was a rehearsal, and its rows."""
    rows = collect_rows(store, run)
    if commands:
        rows = [row for row in rows if is_command_row(row)]
    return {
        "run_id": run.run_id,
        "workflow": run.workflow_name,
        "status": run.status.value,
        # a rehearsal ran nothing: no shell body, no provider call. A ledger that reads like a
        # completed run would be evidence for work that never happened.
        "dry_run": run.dry_run,
        "actor": None if run.actor is None else run.actor.model_dump(mode="json"),
        "workdir": run.workspace.workdir,
        "branch": run.workspace.branch,
        "rows": rows,
    }


def _stamp(row: dict[str, Any]) -> str:
    return _row_time(row).astimezone(UTC).strftime("%H:%M:%S")


def rows_table(rows: list[dict[str, Any]]) -> Table:
    """The ledger table (time · what · step · detail); every cell is plain, safe text."""
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False)
    table.add_column("time", style="dim", no_wrap=True)
    table.add_column("what", no_wrap=True)
    table.add_column("step", style="dim", no_wrap=True)
    table.add_column("detail", overflow="fold")
    for row in rows:
        kind = str(row.get("kind") or "")
        table.add_row(
            _stamp(row),
            Text(safe_text(kind, keep_newlines=False), style=ROW_STYLES.get(kind, "")),
            Text(safe_text(row.get("step") or "", keep_newlines=False)),
            Text(safe_text(row.get("detail") or "", keep_newlines=False)),
        )
    return table


def print_audit(out: Console, store: FileRunStore, run: RunRecord, *, commands: bool) -> None:
    """Header (run, actor, workspace) plus the ledger table, or a note when it is empty.

    A ``--dry-run`` rehearsal is marked in the header: it executed no shell body and called no
    provider, so its rows must never be mistaken for a record of work that happened.
    """
    payload = audit_payload(store, run, commands=commands)
    out.print(
        Text.assemble(
            (f"{run.run_id}  ", "bold"),
            (f"{safe_text(run.workflow_name, keep_newlines=False)}  ", ""),
            (run.status.value, "dim"),
            ("  [dry run — nothing was executed]" if run.dry_run else "", "bold yellow"),
        )
    )
    out.print(Text.assemble(("actor: ", "dim"), actor_line(run)))
    if run.workspace.workdir:
        where = safe_text(run.workspace.workdir, keep_newlines=False)
        if run.workspace.branch:
            where += f" (branch {safe_text(run.workspace.branch, keep_newlines=False)})"
        out.print(Text.assemble(("workdir: ", "dim"), where))
    rows = payload["rows"]
    if not rows:
        out.print(
            "[dim](nothing recorded for this run"
            + (" that is a command" if commands else "")
            + ")[/dim]"
        )
        return
    out.print(rows_table(rows))


def register(app: typer.Typer) -> None:
    @app.command()
    def audit(
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        commands: Annotated[
            bool,
            typer.Option(
                "--commands",
                help="Only what was executed: agent commands and shell/python steps.",
            ),
        ] = False,
        json_: JsonOption = False,
        output: OutputOption = None,
        root: RootOption = None,
    ) -> None:
        """Show what a run did: commands, tools, files, warnings and approvals."""
        json_ = resolve_output(output, json_)
        ctx = common.make_runs_context(root)
        store, record = common.lookup_run(ctx, run)
        out = console()
        if json_:
            payload = audit_payload(store, record, commands=commands)
            out.print(
                json.dumps(payload, ensure_ascii=False, default=str), markup=False, highlight=False
            )
            return
        print_audit(out, store, record, commands=commands)


__all__ = [
    "COMMAND_STEP_KINDS",
    "ROW_STYLES",
    "actor_line",
    "audit_payload",
    "collect_rows",
    "is_command_row",
    "print_audit",
    "register",
    "rows_table",
]
