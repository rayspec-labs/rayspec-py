# SPDX-License-Identifier: Apache-2.0
"""`rayspec audit <run> [--commands] [--json]` — what that run actually did, as a ledger.

One line per fact the run left behind, in time order: the run itself (started, workspace,
paused, finished), every step, every command an agent ran, every tool it called, every file it
reported changing, every warning, and every approval with the identity behind it. It is the
question ``logs --stream`` answers only if you read the whole transcript with a careful eye.

Module boundary: **read-only**. This command opens ``run.json``, ``events.jsonl`` and the step
``stream.jsonl`` files through the store and prints them; it never writes, never re-runs
anything and never contacts anything. The row shape is the store's
(:func:`rayspec.store.file.audit_entry_for_create` / ``audit_entry_for_event`` /
``audit_entry_for_stream``), which is also what an enabled ``audit.jsonl`` holds — so a
rendered ledger and a stored one always agree.

Honest limits: this is a report over the files of ONE run on THIS machine. It proves nothing
about them — anybody who can read the run directory can also edit it — and it has no notion of
other runs, other projects or other people.
"""

from __future__ import annotations

from collections.abc import Iterable
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
    new_table,
    print_json,
    resolve_output,
)
from rayspec.store.file import (
    AUDIT_STREAM_KINDS,
    FileRunStore,
    audit_entry_for_create,
    audit_entry_for_event,
    audit_entry_for_stream,
    finish_audit_row,
    is_attempt_start_row,
    is_step_end_row,
    is_step_start_row,
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


def command_step_paths(run: RunRecord) -> frozenset[str]:
    """The paths of this run's ``shell:``/``python:`` steps — the steps that ARE a command.

    Read off the ``kind`` of the step **record** (``run.json``), not off a row payload: only the
    ``step.started`` event repeats the kind, so a filter that reads the payload alone keeps a
    shell step's start and drops its ``succeeded``/``failed``. Asking the record answers for
    every row of the step at once.

    This is only half of ``--commands``. Being a command step says nothing about whether any one
    of its rows reports a command that ran — that is :func:`command_rows`.
    """
    return frozenset(path for path, rec in run.steps.items() if rec.kind in COMMAND_STEP_KINDS)


def command_rows(run: RunRecord, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``--commands`` view of ``rows``: only what this run executed.

    **The rule, in one sentence:** a row is in this view iff it is a ``command`` row, or it is a
    row of a ``shell:``/``python:`` step that lies inside one of that step's executions — the
    span a ``step.started`` row opens (:func:`~rayspec.store.file.is_step_start_row`) and the
    step's next end row (:func:`~rayspec.store.file.is_step_end_row`) or the next attempt
    (:func:`~rayspec.store.file.is_attempt_start_row`) closes.

    That is a question about the ROW, and it is total. The three brackets are event TYPES the
    engine writes, never a status, a skip reason or a replay marker, so a status invented next
    year is answered without this function changing:

    * a step decided against before it began — ``when: false``, an upstream failure, the budget
      breaker — has an end row and no start to close, so it is outside every execution;
    * a step a resume replayed from its cache likewise has an end row of its own: the earlier
      attempt's execution was closed by the earlier attempt's end row, and nothing ran this time;
    * a retry is an execution and stays inside its start's bracket, so every attempt is kept;
    * an execution the ledger never saw end — the process was killed between the two rows — is
      closed by the attempt boundary, because no execution outlives the process that opened it.
      Its start row stays, since the run really did start executing there, and it is the last
      row of that execution: what the killed attempt wrote is what the view shows of it;
    * the same step can execute, be skipped and be replayed across the attempts of one run, and
      each of its rows is answered on its own — which is the whole of what a step-wide answer
      (does this run have a ``step.started`` for it *anywhere*?) got wrong on a resumed run.

    Asking per row is also what makes a shell step's *outcome* survive the filter next to its
    start, where matching on the payload's ``kind`` would keep only the start.

    A ``--dry-run`` rehearsal is the one whole-run answer: it called no provider and ran no shell
    body, so nothing it recorded was executed and the view is empty. The engine still brackets
    every step it rehearsed — a rehearsal is how a run is shaped and the ledger records that
    shaping — which is exactly why the view cannot be left to read those brackets as work. The
    printed header says the same thing (``dry run — nothing was executed``).

    The brackets are read left to right, so this is defined over the ledger **in the order the
    engine wrote it**, which :func:`collect_rows` reconstructs from the timestamps the writer
    stamps at append time. A store that stopped appending in timestamp order, or a clock that
    went backwards between two attempts, would reorder the rows and invalidate the brackets.
    """
    if run.dry_run:
        return []
    commands = command_step_paths(run)
    executing: set[str] = set()  # step paths whose execution the ledger has open
    kept: list[dict[str, Any]] = []
    for row in rows:
        if is_attempt_start_row(row):  # a new process: nothing it finds open can still be running
            executing.clear()
            continue
        if row["kind"] == "command":  # a command an agent ran: it ran, whoever reported it
            kept.append(row)
            continue
        if row["kind"] != "step":
            continue
        path = str(row.get("step") or "")
        inside = path in executing
        if is_step_start_row(row):
            executing.add(path)
            inside = True
        elif inside and is_step_end_row(row):
            executing.discard(path)
        if inside and path in commands:
            kept.append(row)
    return kept


def collect_rows(store: FileRunStore, run: RunRecord) -> list[dict[str, Any]]:
    """Every ledger row of one run, oldest first.

    The first row is the run being created (``run.json`` is the only source for it: no event
    carries the actor). Lifecycle events then come from ``events.jsonl`` and the per-step
    records from each recorded step's ``stream.jsonl``; all three are mapped through the
    store's own row derivation, so this renders exactly what an enabled ``audit.jsonl`` would
    have stored. Rows are sorted by timestamp, ties keeping the order they were read in.

    Only the record kinds the ledger keeps are parsed (``AUDIT_STREAM_KINDS``): a transcript is
    megabytes of deltas and this command must not be more expensive than reading it. A step
    whose stream cannot be read becomes a visible ``warning`` row — in a report about what a
    run did, a source that could not be read must never look like a step that did nothing.
    """
    rows: list[dict[str, Any]] = [finish_audit_row(audit_entry_for_create(run))]
    for event in store.read_events(run.run_id):
        entry = audit_entry_for_event(event)
        if entry is not None:
            rows.append(finish_audit_row(entry))
    for path in run.steps:
        try:
            for record in store.read_stream(run.run_id, path, kinds=AUDIT_STREAM_KINDS):
                entry = audit_entry_for_stream(path, record)
                if entry is not None:
                    rows.append(finish_audit_row(entry))
        except (OSError, ValueError) as exc:
            rows.append(unreadable_row(run, path, exc))
    rows.sort(key=_row_time)
    return rows


def unreadable_row(run: RunRecord, step_path: str, exc: Exception) -> dict[str, Any]:
    """A ``warning`` row saying a step's records could not be read, and why.

    It is stamped with the run's creation time so it sorts to the top of the ledger: the reader
    has to know that what follows is incomplete before reading it.
    """
    return finish_audit_row(
        {
            "ts": run.created_at.isoformat(),
            "kind": "warning",
            "step": step_path,
            "detail": f"could not read this step's records: {exc}",
            "data": {"unreadable": True},
        }
    )


def _row_time(row: dict[str, Any]) -> datetime:
    """Parse a row's ``ts`` for sorting; an unreadable stamp sorts first, never raises."""
    try:
        moment = datetime.fromisoformat(str(row["ts"]))
    except (KeyError, TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def actor_line(run: RunRecord) -> str:
    """``launcher@example.com (env, ci: github-actions)`` — who ran it, ``—`` when unrecorded.

    A ``declared_id`` is appended as what it is: an identity a ``.env`` file asked for and did
    not get, because a workflow step can write that file. Showing it next to the identity that
    WAS used is the difference between a refusal and a silence.
    """
    actor = run.actor
    if actor is None:
        return "— (not recorded)"
    extras = [actor.source]
    if actor.ci:
        extras.append(f"ci: {actor.ci}")
    for provider, account in sorted(actor.provider_accounts.items()):
        extras.append(f"{provider}: {account}")
    line = f"{safe_text(actor.id, keep_newlines=False)} ({', '.join(safe_text(e) for e in extras)})"
    if actor.declared_id:
        declared = safe_text(actor.declared_id, keep_newlines=False)
        line += f" — a .env declared {declared!r}, which is not an identity"
    return line


def audit_payload(store: FileRunStore, run: RunRecord, *, commands: bool) -> dict[str, Any]:
    """The ``--json`` object: the run's identity, whether it was a rehearsal, and its rows."""
    rows = collect_rows(store, run)
    if commands:
        rows = command_rows(run, rows)
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
    """``10:00:00 UTC`` — the same rendering ``rayspec logs`` prints, zone included: an audit row
    is evidence, and evidence that does not say whose clock it is worth less."""
    return common.fmt_clock(_row_time(row))


def rows_table(rows: list[dict[str, Any]]) -> Table:
    """The ledger table (time · what · step · detail); every cell is plain, safe text."""
    table = new_table()
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
            print_json(audit_payload(store, record, commands=commands))
            return
        print_audit(out, store, record, commands=commands)


__all__ = [
    "COMMAND_STEP_KINDS",
    "ROW_STYLES",
    "actor_line",
    "audit_payload",
    "collect_rows",
    "command_rows",
    "command_step_paths",
    "print_audit",
    "register",
    "rows_table",
    "unreadable_row",
]
