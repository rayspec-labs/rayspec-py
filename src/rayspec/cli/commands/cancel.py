# SPDX-License-Identifier: Apache-2.0
"""`rayspec cancel <run> [--yes] [--force] [--mark] [--json]` — stop a live run or cancel a
paused one.

Live run (status ``running`` with a live pid on this host): the pid is first verified to be
*this run's* rayspec process (its command line names ``rayspec`` and the run id / workflow;
pid reuse after a crash or an edited record is refused with exit 2 and a ``--mark`` hint),
then, after confirmation, SIGINT is sent — the engine finalizes the record itself (steps
``interrupted``, exit 130 in that process). Paused run, a ``running`` record whose process is
gone, or ``--mark``: the record is marked ``cancelled`` here (``pid`` cleared, ``run.finished``
appended to ``events.jsonl``) and the workdir lock is released best effort — nothing is
signalled. A ``running`` record that belongs to another host (shared ``RAYSPEC_HOME``) cannot be
probed from here and is refused unless ``--force``. Step records are left as they are so a later
``rayspec resume`` behaves like a resume of any cancelled run.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.text import Text

from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import JsonOption, RootOption, console, fail
from rayspec.events.model import EventType, RunEvent
from rayspec.schema import RunStatus
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord, utcnow


def mark_cancelled(store: FileRunStore, run: RunRecord, *, reason: str) -> None:
    """Finalize ``run`` as cancelled in the store (record + ``run.finished`` event)."""
    run.status = RunStatus.CANCELLED
    run.reason = reason
    run.ended_at = utcnow()
    run.pid = None
    store.save(run)
    usage = run.total_usage()
    store.append_event(
        run.run_id,
        RunEvent(
            type=EventType.RUN_FINISHED,
            run_id=run.run_id,
            data={
                "status": RunStatus.CANCELLED.value,
                "reason": reason,
                "usage": common.usage_dict(usage),
                "cost_usd": run.total_cost_usd(),
                "outputs": None,
            },
        ),
    )


def register(app: typer.Typer) -> None:
    @app.command()
    def cancel(  # noqa: PLR0917 - Typer options are positional by construction
        run: Annotated[str, typer.Argument(help="Run id or unique prefix.")],
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Do not ask before interrupting a live run.")
        ] = False,
        force: Annotated[
            bool,
            typer.Option(
                "--force",
                help="Mark a running record cancelled even if it belongs to another host.",
            ),
        ] = False,
        mark: Annotated[
            bool,
            typer.Option(
                "--mark",
                help="Mark the record cancelled without signalling any process (stale record, "
                "pid reused by another program); no confirmation prompt.",
            ),
        ] = False,
        json_: JsonOption = False,
        root: RootOption = None,
    ) -> None:
        """Interrupt a live run (SIGINT) or mark a paused run cancelled."""
        ctx = common.make_runs_context(root)
        store, record = common.lookup_run(ctx, run)
        out = console()
        payload: dict[str, Any]
        if (
            record.status is RunStatus.RUNNING
            and common.on_other_host(record)
            and not force
            and not mark
        ):
            fail(
                f"run {record.run_id} is recorded as running on host {record.host} "
                f"(pid {record.pid or '?'}) — its process cannot be checked from here",
                hint="cancel it on that host, or pass --force if you are sure it is dead",
            )
            return
        if record.status in {RunStatus.RUNNING, RunStatus.PAUSED} and mark:
            pid = record.pid
            reason = (
                f"marked cancelled by rayspec cancel --mark (recorded process {pid or '?'} "
                f"on {record.host or '?'} was not signalled)"
            )
            mark_cancelled(store, record, reason=reason)
            released = common.release_workdir_lock(ctx, record)
            payload = {
                "run_id": record.run_id,
                "action": "marked",
                "pid": None,
                "status": record.status.value,
                "lock_released": released,
            }
            if json_:
                out.print(json.dumps(payload), markup=False, highlight=False)
            else:
                out.print(
                    Text.assemble(
                        (f"run {record.run_id} marked cancelled", "yellow"), f" — {reason}"
                    )
                )
            return
        if record.status is RunStatus.RUNNING and common.pid_alive(record):
            assert record.pid is not None
            if not common.pid_is_rayspec_run(record):
                fail(
                    f"pid {record.pid} is not a rayspec run process (stale record?) — use "
                    "`rayspec cancel --mark` to mark the run cancelled without signalling",
                    hint=f"rayspec cancel {record.run_id} --mark",
                )
                return
            if not yes and not json_:
                prompt = f"interrupt run {record.run_id} (pid {record.pid} on {record.host})?"
                try:
                    confirmed = typer.confirm(prompt, default=False)
                except typer.Abort:
                    # no terminal to answer on (stdin closed / not a TTY): a usage error, not
                    # "the run failed"
                    fail(
                        f"cannot confirm interrupting run {record.run_id} without a terminal",
                        hint="pass --yes to interrupt without confirmation",
                    )
                    return
                if not confirmed:
                    fail("aborted — the run keeps running", code=1)
                    return
            try:
                common.interrupt_pid(record.pid)
            except OSError as exc:
                fail(f"cannot signal pid {record.pid}: {exc}")
                return
            payload = {
                "run_id": record.run_id,
                "action": "signalled",
                "pid": record.pid,
                "status": record.status.value,
            }
            if json_:
                out.print(json.dumps(payload), markup=False, highlight=False)
            else:
                out.print(
                    f"sent SIGINT to pid {record.pid} — run {record.run_id} is shutting down "
                    f"(watch with `rayspec logs {record.run_id} --follow`)",
                    markup=False,
                )
            return
        if record.status is RunStatus.PAUSED:
            reason = "cancelled by rayspec cancel while awaiting approval"
        elif record.status is RunStatus.RUNNING:
            reason = (
                "cancelled by rayspec cancel (recorded process "
                f"{record.pid or '?'} on {record.host or '?'} is no longer running)"
            )
        else:
            fail(
                f"run {record.run_id} is {record.status.value} — nothing to cancel",
                hint="only running or paused runs can be cancelled",
            )
            return
        mark_cancelled(store, record, reason=reason)
        released = common.release_workdir_lock(ctx, record)
        payload = {
            "run_id": record.run_id,
            "action": "cancelled",
            "pid": None,
            "status": record.status.value,
            "lock_released": released,
        }
        if json_:
            out.print(json.dumps(payload), markup=False, highlight=False)
        else:
            out.print(Text.assemble((f"run {record.run_id} cancelled", "yellow"), f" — {reason}"))


__all__ = ["mark_cancelled", "register"]
