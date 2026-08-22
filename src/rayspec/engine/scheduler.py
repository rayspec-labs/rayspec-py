# SPDX-License-Identifier: Apache-2.0
"""Scheduler: ``run_graph`` (one sibling list as a DAG) and ``run_one`` (one step).

Module boundary: ordering, join/when/drain decisions, the leaf retry loop and the
"every outcome is a value" guarantee. Executors (``executors/``) do the actual work; the
:class:`~rayspec.engine.context.RunContext` persists and emits.

Semantics (plan §3.2):

* a step is *considered* only when ALL its ``needs`` are terminal → budget gate (once the
  run-level cap tripped a pending step is replayed from the resume cache when it can be, else
  recorded ``skipped``/``budget_exceeded``; ``join: always`` exempt) → :func:`join_decision` →
  drain check (``join: always`` exempt) → ``when:`` (strict bool; eval error ⇒ **failed**) →
  launch; ``skipped`` is terminal and cascades synchronously;
* failure policy is local to each graph: default **drain** (no new launches except ``always``,
  running siblings finish); ``--fail-fast`` cancels the graph's task group (running siblings
  become ``interrupted``);
* the ``max_parallel`` permit is held ONLY around prompt/shell/python executors;
* ``stop:`` / pause signals (``RunControl``) cancel the siblings (``interrupted``, reason
  ``stopped``/``paused``) and bubble after the graph finishes — one signal per sibling list, the
  first one, except that a pause beats a stop; cancellation from outside marks
  running steps ``interrupted`` (except a gate that already recorded a pause: Ctrl-C at the
  prompt keeps the record ``paused``);
* ``run_one`` never raises (except to propagate cancellation): bugs become failed steps.
"""

from __future__ import annotations

import contextlib
import math
from typing import Any

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from rayspec.engine.context import (
    BUDGET_SKIP_REASON,
    LEAF_KINDS,
    REUSABLE_KINDS,
    ExecScope,
    ExecutorFn,
    RunContext,
    StepOutcome,
    error_info,
    failed_outcome,
    merge_cost_source,
    utcnow,
    view_of,
)
from rayspec.engine.errors import RunControl, RunPaused
from rayspec.engine.executors.artifacts import collect_artifacts
from rayspec.engine.graph import FAILED_LIKE, StepGraph, join_decision
from rayspec.engine.retry import TIMEOUT_ERROR_TYPE, delay_for, policy_for, should_retry
from rayspec.events.model import EventType
from rayspec.providers.base import Usage, usage_dict
from rayspec.schema import ApproveStep, StepModel, StepStatus
from rayspec.store.model import ErrorInfo, StepRecord
from rayspec.templating import TemplateRenderError


def _executor_for(ctx: RunContext, kind: str) -> ExecutorFn:
    executor = ctx.executors.get(kind)
    if executor is not None:
        return executor
    from rayspec.engine.executors import default_executors

    return default_executors()[kind]


# --------------------------------------------------------------------------------------------------
# graphs
# --------------------------------------------------------------------------------------------------


async def run_graph(graph: StepGraph, scope: ExecScope, ctx: RunContext) -> dict[str, StepOutcome]:
    """Run one sibling list to completion; returns the outcome of every step that was decided.

    Raises :class:`RunStopped` / :class:`RunPaused` after the graph has wound down when a step
    signalled them; propagates cancellation (after marking running steps ``interrupted``).
    """
    outcomes: dict[str, StepOutcome] = {}
    pending: dict[str, StepModel] = {s.id: s for s in graph.steps}
    running: set[str] = set()
    state: dict[str, Any] = {"draining": False, "control": None, "paused": False}
    send, recv = anyio.create_memory_object_stream[StepOutcome](math.inf)
    # The failure policy of THIS sibling list: an ``include:``d workflow that states its own
    # ``defaults.on_step_failure`` governs its body, one that says nothing inherits the run's.
    keep_going = ctx.keep_going_for(scope)
    fail_fast = ctx.fail_fast_for(scope)

    def settle(outcome: StepOutcome) -> None:
        sid = outcome.record.id
        outcomes[sid] = outcome
        scope.views[sid] = view_of(outcome, graph.by_id.get(sid))
        rec = outcome.record
        vetoed = rec.error is not None and rec.error.type == "rejected"
        if rec.status in FAILED_LIKE and not rec.tolerated and (vetoed or not keep_going):
            # ``on_step_failure: continue`` keeps the ready-set open; dependents of this
            # step still skip (``upstream_failed`` is decided before ``draining`` in join_decision).
            # CARVE-OUT: a human rejecting a gate (``on_reject: fail``) always drains. ``continue``
            # is for triaging machine failures, not for overriding an operator's "no".
            state["draining"] = True
        if rec.status is StepStatus.PAUSED:
            # not a terminal outcome: nothing that needs this step can be decided any more, and
            # the wind-down has to stop where it is (the resumed run picks the list back up)
            state["draining"] = True
            state["paused"] = True
        control = outcome.control
        if control is not None and (
            state["control"] is None
            or (isinstance(control, RunPaused) and not isinstance(state["control"], RunPaused))
        ):
            # first signal wins, except that a pause beats a stop — the same rule the ``each:``
            # executor applies to its items. A pause keeps the run resumable, and a gate that
            # paused during the wind-down of a ``stop:`` must not be buried by that ``stop:``.
            state["control"] = control

    def decidable(sid: str) -> bool:
        """Whether every ``needs`` of ``sid`` has settled on a TERMINAL outcome.

        A record can also settle non-terminal — an ``approve:`` gate that paused, or a composite
        whose body paused — and the join table has no row for that. Such a step is simply not
        considered: a pause leaves it pending for the resumed run to decide.
        """
        return all(
            n in outcomes and outcomes[n].record.status.is_terminal for n in graph.needs[sid]
        )

    async def decide_and_launch(tg: TaskGroup) -> None:
        progressed = True
        while progressed:
            progressed = False
            for sid in list(pending):
                step = pending[sid]
                needs = graph.needs[sid]
                if not decidable(sid):
                    continue
                if ctx.budget_exceeded is not None and step.join != "always":
                    # the run-level cap tripped — nothing new starts (running steps drain,
                    # ``join: always`` steps still run); a resume replay is free, so a reusable
                    # record is replayed instead of being overwritten as skipped
                    del pending[sid]
                    record = ctx.new_record(step, scope)
                    outcome = await try_reuse(step, scope, ctx, record)
                    if outcome is None:
                        outcome = _skipped(record, BUDGET_SKIP_REASON)
                    await finish(outcome, step, scope, ctx)
                    settle(outcome)
                    progressed = True
                    continue
                draining = bool(state["draining"]) or state["control"] is not None
                decision = join_decision(
                    step, [outcomes[n].record for n in needs], draining=draining
                )
                if not decision.run:
                    del pending[sid]
                    outcome = _skipped(ctx.new_record(step, scope), decision.skip_reason or "")
                    await finish(outcome, step, scope, ctx)
                    settle(outcome)
                    progressed = True
                    continue
                if step.when is not None:
                    verdict = _evaluate_when(step, scope, ctx)
                    if verdict is not True:
                        del pending[sid]
                        record = ctx.new_record(step, scope)
                        outcome = (
                            _skipped(record, "when_false")
                            if verdict is False
                            else failed_outcome(record, verdict)
                        )
                        await finish(outcome, step, scope, ctx)
                        settle(outcome)
                        progressed = True
                        continue
                del pending[sid]
                running.add(sid)
                tg.start_soon(run_one, step, scope, ctx, send)

    def collect() -> None:
        """Settle every outcome the stream already holds (the senders are all done)."""
        while True:
            try:
                outcome = recv.receive_nowait()
            except (anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
                return
            running.discard(outcome.record.id)
            settle(outcome)

    async def wind_down(reason: str, *, cancelled: bool) -> None:
        """Decide the steps still pending after the graph was torn down.

        The graph's task group is gone — fail-fast cancelled it, or a ``stop:`` did — but the
        last row of the join table says a ``join: always`` step runs when the run is draining
        *or* cancelled, and that row is the whole point of the finally idiom. So the leftovers
        are not blanket-skipped: they are decided in dependency order, ``always`` steps run in a
        fresh task group and everything else is skipped.

        The skip reason: after a **cancellation** every leftover is ``stopped``, because nothing
        on those branches failed — the run was called off, and ``upstream_failed`` would read as
        a failure that did not happen. After **fail-fast** a failure is exactly what happened, so
        a skip that the step's own ``needs`` explain keeps that reason (``upstream_failed`` /
        ``upstream_skipped``) and only the rest is ``run_failed``.

        Nothing here cancels: a cleanup step that fails must not take the other cleanup steps
        with it. A cleanup step that PAUSES ends the wind-down and leaves the rest pending, so
        the resumed run decides them — and it is the pause that bubbles, not the signal that
        started the teardown, so the run stays answerable (``paused``, exit 3) instead of
        recording an outcome no ``rayspec approve`` could ever act on.
        """
        while pending:
            if state["paused"]:
                return
            ready = [sid for sid in pending if decidable(sid)]
            if not ready:  # unreachable for a DAG whose other steps are all decided
                return
            launch: list[str] = []
            for sid in ready:
                step = pending[sid]
                decision = join_decision(
                    step, [outcomes[n].record for n in graph.needs[sid]], draining=True
                )
                if decision.run:
                    launch.append(sid)
                    continue
                del pending[sid]
                explained = not cancelled and decision.skip_reason not in (None, "run_failed")
                skip_reason = decision.skip_reason if explained else reason
                outcome = _skipped(ctx.new_record(step, scope), skip_reason or reason)
                await finish(outcome, step, scope, ctx)
                settle(outcome)
            if not launch:
                continue
            async with anyio.create_task_group() as tg:
                for sid in launch:
                    step = pending.pop(sid)
                    if step.when is not None:
                        verdict = _evaluate_when(step, scope, ctx)
                        if verdict is not True:
                            record = ctx.new_record(step, scope)
                            outcome = (
                                _skipped(record, "when_false")
                                if verdict is False
                                else failed_outcome(record, verdict)
                            )
                            await finish(outcome, step, scope, ctx)
                            settle(outcome)
                            continue
                    running.add(sid)
                    tg.start_soon(run_one, step, scope, ctx, send)
            collect()

    try:
        async with anyio.create_task_group() as tg:
            await decide_and_launch(tg)
            while running:
                outcome = await recv.receive()
                running.discard(outcome.record.id)
                settle(outcome)
                control = state["control"]
                if control is not None:
                    scope.cancel_reason = cancel_reason_for(control)
                    tg.cancel_scope.cancel()
                    break
                if state["draining"] and fail_fast and running:
                    scope.cancel_reason = "failed"
                    tg.cancel_scope.cancel()
                    break
                await decide_and_launch(tg)
    finally:
        # outcomes of siblings cancelled by a control signal / fail-fast / outer cancellation
        collect()
    control = state["control"]
    if control is not None and not isinstance(control, RunPaused):
        # a stop / reject cancelled the graph: the leftovers are decided, the cleanup ones run
        await wind_down(STOPPED_REASON, cancelled=True)
    elif control is None and state["draining"] and fail_fast:
        await wind_down("run_failed", cancelled=False)
    # a cleanup step may have signalled a stop or a pause of its own (only if nothing had yet)
    control = state["control"]
    if control is not None:
        raise control
    return outcomes


#: The ``skip_reason`` a step carries when a ``stop:`` cancelled it while the sibling list was
#: torn down. It is the marker the runner reads to tell a stop's own teardown from a failure that
#: happened on its own (:func:`rayspec.engine.runner.stop_collateral`) — one name, so the two
#: sides can never drift apart.
STOPPED_REASON = "stopped"
#: The same, for a pause.
PAUSED_REASON = "paused"


def cancel_reason_for(control: RunControl) -> str:
    """The ``skip_reason`` of steps interrupted because of ``control`` (``paused``/``stopped``)."""
    return PAUSED_REASON if isinstance(control, RunPaused) else STOPPED_REASON


def _evaluate_when(step: StepModel, scope: ExecScope, ctx: RunContext) -> bool | ErrorInfo:
    assert step.when is not None
    try:
        return ctx.engine.eval_bool(step.when, ctx.template_context(scope))
    except TemplateRenderError as exc:
        return error_info(exc, type_="when")
    except Exception as exc:  # a bug in evaluation is still a failed step, not a crash
        return error_info(exc, type_="when")


# --------------------------------------------------------------------------------------------------
# one step
# --------------------------------------------------------------------------------------------------


async def run_one(
    step: StepModel,
    scope: ExecScope,
    ctx: RunContext,
    send: MemoryObjectSendStream[StepOutcome],
) -> None:
    """Run one step and send its :class:`StepOutcome`; never raises (cancellation propagates)."""
    record = ctx.new_record(step, scope)
    outcome: StepOutcome | None = None
    try:
        outcome = await _execute(step, scope, ctx, record)
    except anyio.get_cancelled_exc_class():
        if record.status is StepStatus.PAUSED:
            # Ctrl-C at an approval prompt: the gate already recorded the pause (plan: "Ctrl-C
            # at a prompt = pause"); keep it instead of overwriting it as interrupted
            outcome = StepOutcome(record=record, control=ctx.paused)
        else:
            outcome = _interrupted(record, scope.cancel_reason or "interrupted")
        with anyio.CancelScope(shield=True):
            await _finish_quietly(outcome, step, scope, ctx)
        send.send_nowait(outcome)
        raise
    except RunControl as exc:
        outcome = _controlled(record, exc)
    except Exception as exc:  # bugs become failed steps, not crashes
        outcome = failed_outcome(record, error_info(exc, type_="engine"))
    try:
        await finish(outcome, step, scope, ctx)
    except anyio.get_cancelled_exc_class():
        with anyio.CancelScope(shield=True):
            await _finish_quietly(outcome, step, scope, ctx)
        send.send_nowait(outcome)
        raise
    except Exception as exc:
        outcome.record.status = StepStatus.FAILED
        outcome.record.error = error_info(exc, type_="persist")
        outcome.record.ok = False
        outcome.record.tolerated = False
    send.send_nowait(outcome)


async def _execute(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord
) -> StepOutcome:
    reused = await try_reuse(step, scope, ctx, record)
    if reused is not None:
        return reused
    kind = type(step).kind
    await ctx.emit(
        EventType.STEP_STARTED, step_path=record.path, kind=kind, attempt=record.attempts + 1
    )
    await ctx.save_record(record)
    outcome = await _dispatch(step, scope, ctx, record, kind)
    # the files the step promised under ``artifacts:`` must be there — a broken promise turns a
    # succeeded outcome into a failed one (never a retry: the file is missing, not flaky)
    return await collect_artifacts(step, scope, ctx, outcome)


async def _dispatch(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, kind: str
) -> StepOutcome:
    """Hand the step to its executor: leaf retry loop, gate, or one attempt with a timeout."""
    executor = _executor_for(ctx, kind)
    if kind in LEAF_KINDS:
        return await run_leaf(step, scope, ctx, record, executor)
    if isinstance(step, ApproveStep):
        # the gate manages attempts itself (a consumed decision answers the paused attempt)
        return await executor(step, scope, ctx, record, record.attempts + 1)
    record.attempts += 1
    timeout = ctx.timeout_for(step, scope)
    try:
        with anyio.fail_after(timeout):
            return await executor(step, scope, ctx, record, record.attempts)
    except TimeoutError:
        return failed_outcome(
            record,
            ErrorInfo(
                type=TIMEOUT_ERROR_TYPE, message=f"timed out after {timeout:g}s", transient=False
            ),
        )


async def run_leaf(
    step: StepModel,
    scope: ExecScope,
    ctx: RunContext,
    record: StepRecord,
    executor: ExecutorFn,
) -> StepOutcome:
    """The leaf attempt loop: permit → per-attempt timeout → retry per policy."""
    policy = policy_for(step)
    attempts_this_run = 0
    # usage / cost are summed over every attempt — including the ones of earlier runs that
    # ``ctx.new_record`` carried over (an interrupted attempt's tokens stay counted)
    usage_total = record.usage
    cost_total: float | None = record.cost_usd
    source_total = record.cost_source if record.cost_usd is not None else "none"

    def accumulate(rec: StepRecord) -> None:
        """Fold the attempt-scoped usage/cost on ``rec`` into the running totals."""
        nonlocal usage_total, cost_total, source_total
        usage_total = usage_total + rec.usage
        if rec.cost_usd is not None:
            cost_total = (cost_total or 0.0) + rec.cost_usd
            source_total = merge_cost_source(source_total, rec.cost_source)
        rec.usage = usage_total
        rec.cost_usd = cost_total
        rec.cost_source = source_total

    last: StepOutcome | None = None
    while True:
        timeout = ctx.timeout_for(step, scope)
        async with ctx.runtime.leaf_permit():
            # Only once the permit is held does the attempt exist: a leaf cancelled while it
            # queues for a ``max_parallel`` slot / the launch gate keeps the totals its record
            # carries (earlier attempts, the previous run) and is not counted as an attempt.
            if step.join != "always" and await ctx.check_budget(pending=record) is not None:
                # The run-level cap may have tripped while this attempt waited for a slot. The
                # ready-set gate cannot see a step that is already queued, and a queued step has
                # not started — so the breaker is ASKED again here (not just read: the clock can
                # run out with no step finishing), or a wall-clock cap would keep launching the
                # backlog long after it ran out. A retry keeps the failure it already has; a
                # first attempt is recorded like any other step the cap skipped.
                return last if last is not None else _skipped(record, BUDGET_SKIP_REASON)
            record.attempts += 1
            attempts_this_run += 1
            attempt = record.attempts
            # the executor reports THIS attempt's usage/cost on the record; the totals are
            # folded back in below (also when the attempt is cancelled from outside)
            record.usage = Usage()
            record.cost_usd = None
            record.cost_source = "none"
            try:
                with anyio.fail_after(timeout):
                    outcome = await executor(step, scope, ctx, record, attempt)
            except TimeoutError:
                outcome = failed_outcome(
                    record,
                    ErrorInfo(
                        type=TIMEOUT_ERROR_TYPE,
                        message=f"attempt {attempt} timed out after {timeout:g}s",
                        transient=False,
                    ),
                )
            except anyio.get_cancelled_exc_class():
                accumulate(record)  # interrupted: keep what the attempt reported so far
                raise
            except RunControl:
                raise
            except Exception as exc:
                outcome = failed_outcome(record, error_info(exc, type_="engine"))
        accumulate(outcome.record)
        last = outcome
        rec = outcome.record
        if rec.status is StepStatus.FAILED and await ctx.check_budget(pending=rec) is not None:
            return outcome  # a retry is a new start — not once the cap tripped
        if rec.status is StepStatus.FAILED and should_retry(policy, attempts_this_run, rec.error):
            assert policy is not None
            delay = delay_for(policy, attempts_this_run)
            await ctx.emit(
                EventType.STEP_RETRY,
                step_path=rec.path,
                attempt=attempt + 1,
                delay_s=delay,
                error=rec.error.model_dump() if rec.error else None,
            )
            await anyio.sleep(delay)
            continue
        return outcome


async def try_reuse(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord
) -> StepOutcome | None:
    """Resume cache: replay a reusable record (status + output file + no ``always_run``).

    Leaf records are also compared by fingerprint (rendered prompt/script + agent): a changed
    upstream output or script re-runs the step with a warning, whatever the workflow hash says.
    """
    kind = type(step).kind
    if kind not in REUSABLE_KINDS or step.always_run:
        return None
    prev = ctx.cache.get(record.path)
    if prev is None or not prev.reusable:
        return None
    if scope.item_sha256 is not None and prev.item_sha256 != scope.item_sha256:
        await ctx.warn(
            f"item changed since the previous run; re-running {record.path}",
            step_path=record.path,
        )
        return None
    try:
        output = ctx.read_output_value(prev)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if kind in LEAF_KINDS and prev.fingerprint is not None:
        # Always compared — not only on a hash mismatch: a forced resume stamps the new hash
        # before it finishes, so an interrupted one would otherwise let a later plain resume
        # replay a record computed from stale upstream output.
        from rayspec.engine.executors import fingerprint_of

        try:
            current = fingerprint_of(step, scope, ctx)
        except Exception:
            current = None
        if current != prev.fingerprint:
            await ctx.warn(
                f"workflow changed for {record.path} (fingerprint mismatch); re-running",
                step_path=record.path,
            )
            return None
    outcome = StepOutcome(
        record=prev,
        output=output,
        output_kind=prev.output_kind,
        reused=True,
        event_data={"reused": True},
    )
    if kind in {"shell", "python"}:
        stderr_log = ctx.step_dir(record.path) / "stderr.log"
        if stderr_log.is_file():
            outcome.stderr = stderr_log.read_text(encoding="utf-8", errors="replace")
    ctx.reused_paths.append(record.path)
    return outcome


# --------------------------------------------------------------------------------------------------
# outcome construction / finishing
# --------------------------------------------------------------------------------------------------


def _skipped(record: StepRecord, reason: str) -> StepOutcome:
    record.status = StepStatus.SKIPPED
    record.skip_reason = reason
    record.ok = None
    return StepOutcome(record=record)


def _interrupted(record: StepRecord, reason: str) -> StepOutcome:
    record.status = StepStatus.INTERRUPTED
    record.skip_reason = reason
    record.ok = False
    record.error = ErrorInfo(type="interrupted", message=reason, transient=False)
    return StepOutcome(record=record)


def _controlled(record: StepRecord, control: RunControl) -> StepOutcome:
    if isinstance(control, RunPaused):
        record.status = StepStatus.PAUSED
        record.skip_reason = PAUSED_REASON
    else:
        record.status = StepStatus.INTERRUPTED
        record.skip_reason = STOPPED_REASON
        record.ok = False
    return StepOutcome(record=record, control=control)


def finalize(outcome: StepOutcome, step: StepModel) -> None:
    """Fill the derived record fields (ok / tolerated / ended_at / duration)."""
    rec = outcome.record
    if rec.status is StepStatus.FAILED:
        rec.ok = False
        if step.allow_failure:
            rec.tolerated = True
        if rec.error is None:
            rec.error = ErrorInfo(type="failed", message="step failed", transient=False)
    elif rec.status is StepStatus.SUCCEEDED:
        if rec.ok is None:
            rec.ok = True
        # a success after a retry must not carry the previous attempt's error — the
        # per-attempt history lives in the step.retry events and the stream, not on the record
        rec.error = None
    if rec.ended_at is None and (rec.status.is_terminal or rec.status is StepStatus.PAUSED):
        rec.ended_at = utcnow()
    if rec.duration_ms is None and rec.started_at is not None and rec.ended_at is not None:
        rec.duration_ms = max(0, int((rec.ended_at - rec.started_at).total_seconds() * 1000))


async def finish(outcome: StepOutcome, step: StepModel, scope: ExecScope, ctx: RunContext) -> None:
    """Finalize, persist (write-ahead) and announce one outcome; updates the scope's views."""
    if not outcome.reused:
        finalize(outcome, step)
    await ctx.persist(outcome)
    scope.views[step.id] = view_of(outcome, step)
    rec = outcome.record
    ctx.accounted_paths.add(rec.path)
    spent = rec.kind in LEAF_KINDS and (rec.usage.total or rec.cost_usd is not None)
    if spent or ctx.time_capped:
        # the totals changed (a fresh leaf or a replayed record) — or the wall clock may have
        # run out while this step ran, which no step's usage would show
        await ctx.check_budget()
    data: dict[str, Any] = {
        "status": str(rec.status.value),
        "duration_ms": rec.duration_ms,
        "usage": usage_dict(rec.usage),
        "cost_usd": rec.cost_usd,
        "error": rec.error.model_dump() if rec.error is not None else None,
        "skip_reason": rec.skip_reason,
        "tolerated": rec.tolerated,
    }
    if rec.cost_source != "none":
        data["cost_source"] = rec.cost_source
    if rec.usage_unknown:
        data["usage_unknown"] = True  # an attempt was cut off before any usage report
    data.update(outcome.event_data)
    await ctx.emit(EventType.STEP_FINISHED, step_path=rec.path, **data)


async def _finish_quietly(
    outcome: StepOutcome, step: StepModel, scope: ExecScope, ctx: RunContext
) -> None:
    with contextlib.suppress(Exception):  # the run is unwinding; nothing better to do
        await finish(outcome, step, scope, ctx)


__all__ = [
    "PAUSED_REASON",
    "STOPPED_REASON",
    "cancel_reason_for",
    "finalize",
    "finish",
    "run_graph",
    "run_leaf",
    "run_one",
    "try_reuse",
]
