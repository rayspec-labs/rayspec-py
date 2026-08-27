# SPDX-License-Identifier: Apache-2.0
"""`rayspec cancel <run> [--now] [--yes] [--force] [--mark] [--json]` — stop a live run or
cancel a paused one.

Live run (status ``running`` with a live pid on this host): the pid is first verified to be
*this run's* rayspec process (its command line names ``rayspec`` and the run id / workflow;
pid reuse after a crash or an edited record is refused with exit 2 and a ``--mark`` hint), then,
after confirmation, a cooperative flag (``cancel.json``, next to ``run.json``) is written — PRD-07
R5. Cancel does not signal the process: the runner checks the flag at step boundaries, lets a
step already in flight finish, runs ``join: always`` cleanup, and finalizes the record itself as
``cancelled`` (exit 4) in that process. This needs no terminal (``--yes``/``--json`` waive the
confirmation exactly as before) and works the same for a foreground or a detached run. ``--now``
is the escape hatch: it SIGINTs the process instead of flagging it (the run ends ``interrupted``,
exit 130) — for a step wedged mid-provider-call with no boundary to reach. Paused run, a ``running``
record whose process is gone, or ``--mark``: the record is marked ``cancelled`` here (``pid``
cleared, ``run.finished`` appended to ``events.jsonl``) and the workdir lock is released best
effort — nothing is signalled or flagged. A ``running`` record that belongs to another host
(shared ``RAYSPEC_HOME``) cannot be probed from here and is refused unless ``--force``. Step
records are left as they are so a later ``rayspec resume`` behaves like a resume of any
cancelled run.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.text import Text

from rayspec.actor import resolve_actor
from rayspec.cli import _runs_common as common
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    RootOption,
    console,
    fail,
    print_json,
    resolve_output,
)
from rayspec.engine.cancel import write_cancel_flag
from rayspec.events.model import EventType, RunEvent
from rayspec.schema import RunStatus
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord, utcnow


def _confirm(prompt: str) -> bool:
    """Ask ``prompt`` (default no); a closed stdin (no terminal) is a usage error, not a yes."""
    try:
        return bool(typer.confirm(prompt, default=False))
    except typer.Abort:
        fail("cannot confirm without a terminal", hint="pass --yes to skip the prompt")
        return False


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
            bool, typer.Option("--yes", "-y", help="Do not ask before cancelling a live run.")
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
        now: Annotated[
            bool,
            typer.Option(
                "--now",
                help="SIGINT the process now instead of flagging it — the run ends interrupted "
                "(exit 130); for a step wedged with no boundary to reach.",
            ),
        ] = False,
        json_: JsonOption = False,
        output: OutputOption = None,
        root: RootOption = None,
    ) -> None:
        """Cancel a run — a cooperative flag by default, `--now` to signal, `--mark` to record."""
        json_ = resolve_output(output, json_)
        ctx = common.make_runs_context(root)
        store, record = common.lookup_run(ctx, run)
        # --mark records the run cancelled without probing or signalling, so it runs BEFORE the
        # reconcile that would otherwise flip a stale record to `interrupted` and change the
        # reported action from "marked" to "cancelled" (D12).
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
                print_json(payload)
            else:
                console().print(
                    Text.assemble(
                        (f"run {record.run_id} marked cancelled", "yellow"), f" — {reason}"
                    )
                )
            return
        payload: dict[str, Any]
        if record.status is RunStatus.RUNNING and common.on_other_host(record) and not force:
            fail(
                f"run {record.run_id} is recorded as running on host {record.host} "
                f"(pid {record.pid or '?'}) — its process cannot be checked from here",
                hint="cancel it on that host, or pass --force if you are sure it is dead",
            )
            return
        # a live process to act on — verified BEFORE reconcile, which (via the start-time probe)
        # would otherwise pre-empt the reused-pid refusal below. A stale-but-alive run is still
        # ``running`` on disk here, so this branch reaches it too (N3).
        if (
            record.status is RunStatus.RUNNING
            and record.pid is not None
            and common.pid_alive(record)
        ):
            assert record.pid is not None
            if not common.pid_is_rayspec_run(record):
                fail(
                    f"pid {record.pid} is not a rayspec run process (stale record?) — use "
                    "`rayspec cancel --mark` to mark the run cancelled without signalling",
                    hint=f"rayspec cancel {record.run_id} --mark",
                )
                return
            if now:
                if (
                    not yes
                    and not json_
                    and not _confirm(f"SIGINT run {record.run_id} (pid {record.pid})?")
                ):
                    fail("aborted — the run keeps running", code=1)
                    return
                common.interrupt_pid(record.pid)
                payload = {
                    "run_id": record.run_id,
                    "action": "signalled",
                    "pid": record.pid,
                    "status": record.status.value,
                }
                if json_:
                    print_json(payload)
                else:
                    console().print(
                        f"SIGINT sent to run {record.run_id} (pid {record.pid}) — interrupts now",
                        markup=False,
                    )
                return
            if not yes and not json_:
                prompt = f"cancel run {record.run_id} (pid {record.pid} on {record.host})?"
                try:
                    confirmed = typer.confirm(prompt, default=False)
                except typer.Abort:
                    # no terminal to answer on (stdin closed / not a TTY, exactly the shape a
                    # detached run's cancel needs): a usage error, not "the run failed"
                    fail(
                        f"cannot confirm cancelling run {record.run_id} without a terminal",
                        hint="pass --yes to cancel without confirmation",
                    )
                    return
                if not confirmed:
                    fail("aborted — the run keeps running", code=1)
                    return
            # PRD-07 R5: cooperative — a flag beside run.json, not a signal. The run's own
            # process notices it at the next step boundary and finalizes itself as cancelled.
            actor = resolve_actor().id
            reason = f"cancelled by rayspec cancel (pid {record.pid} on {record.host or '?'})"
            write_cancel_flag(store.run_dir(record.run_id), reason=reason, actor=actor)
            payload = {
                "run_id": record.run_id,
                "action": "flagged",
                "pid": record.pid,
                "status": record.status.value,
            }
            if json_:
                print_json(payload)
            else:
                console().print(
                    f"cancel requested for run {record.run_id} (pid {record.pid}) — it stops at "
                    f"the next step boundary (watch with `rayspec logs {record.run_id} --follow`)",
                    markup=False,
                )
            return
        # not a live running process to flag/signal: reconcile for an honest status, then decide.
        was_running = record.status is RunStatus.RUNNING
        record = common.reconcile_run(store, record)
        if record.status is RunStatus.PAUSED:
            reason = "cancelled by rayspec cancel while awaiting approval"
        elif record.status is RunStatus.RUNNING:
            reason = (
                "cancelled by rayspec cancel (recorded process "
                f"{record.pid or '?'} on {record.host or '?'} is no longer running)"
            )
        elif record.status is RunStatus.INTERRUPTED and was_running:
            # reconcile just turned a running record with a DEAD or reused pid into `interrupted`
            # (a stale-but-alive one is `reachable` above and never gets here); fold in its reason
            reason = f"cancelled by rayspec cancel ({record.reason or 'the run was interrupted'})"
        else:
            # already interrupted/cancelled/finished on disk — nothing for a cancel to do (D13)
            fail(
                f"run {record.run_id} is {record.status.value} — nothing to cancel",
                hint=f"resume it with `rayspec resume {record.run_id}`"
                if record.status is RunStatus.INTERRUPTED
                else "only running or paused runs can be cancelled",
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
            print_json(payload)
        else:
            console().print(
                Text.assemble((f"run {record.run_id} cancelled", "yellow"), f" — {reason}")
            )


__all__ = ["mark_cancelled", "register"]
