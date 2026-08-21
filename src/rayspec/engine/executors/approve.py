# SPDX-License-Identifier: Apache-2.0
"""``approve:`` executor — the human gate.

Flow: ``--yes`` / dry run ⇒ auto-approve (``by: "--yes"``) · a stored ``pause.decision`` whose
token matches this gate ⇒ consumed · otherwise **quiesce** (close the launch gate, wait until
no leaf is active), then a TTY prompt (injected :class:`ApprovalPrompt`) when interactive, else
write ``PauseInfo{token="<path>#<attempt>"}``, emit ``run.paused`` and signal
:class:`RunPaused` (exit 3). Reject: ``on_reject`` cancel (default) ⇒ step ``rejected`` +
:class:`RunStopped(cancelled)` · continue ⇒ succeeded with ``approved: false`` · fail ⇒ failed.
The step output is the approver's comment (``''`` if none).

Simultaneous gates are handled one at a time (``Runtime.approval_lock`` is held from closing
the launch gate until the decision or pause is recorded); when the run is already pausing at
another gate, a later gate pauses too but does not overwrite ``run.pause`` (the decision slot
belongs to the first gate; the later one asks again on resume).

Module boundary: owns tokens, quiesce and decision recording; asking is the prompt's job.
"""

from __future__ import annotations

import json

import anyio

from rayspec.engine.approval import ApprovalAnswer, ApprovalNeed, ApprovalRequest
from rayspec.engine.context import (
    ExecScope,
    RunContext,
    StepOutcome,
    cost_source_of,
    error_info,
)
from rayspec.engine.errors import RunPaused, RunStopped
from rayspec.events.model import EventType
from rayspec.schema import ApproveStep, StepModel, StepStatus
from rayspec.store.model import Decision, ErrorInfo, PauseInfo, StepRecord
from rayspec.templating import TemplateRenderError

#: Characters of each need's output handed to the prompt for ``[v]iew``.
_VIEW_CAP = 200_000


def gate_token(path: str, attempt: int) -> str:
    """``<path>#<attempt>`` — ties a decision to one specific pause of one gate."""
    return f"{path}#{attempt}"


def stored_decision(ctx: RunContext, path: str, attempt: int) -> Decision | None:
    """The decision recorded for this gate's token (``rayspec approve|reject``), if any."""
    pause = ctx.run.pause
    if pause is None or pause.decision is None or pause.step != path:
        return None
    if pause.token != gate_token(path, attempt):
        return None
    return pause.decision


def run_cost_source(ctx: RunContext) -> str:
    """The run-level cost source the panel renders (``provider`` → ``$``, ``table`` → ``~$``,
    ``partial`` → ``≥$``, ``none``); the engine's :func:`~rayspec.engine.context.cost_source_of`
    rule."""
    return cost_source_of(ctx.run.steps.values())


def _needs_summary(step: ApproveStep, scope: ExecScope, ctx: RunContext) -> list[ApprovalNeed]:
    needs: list[ApprovalNeed] = []
    for need in step.needs:
        view = scope.views.get(need)
        rec = ctx.run.steps.get(str(scope.record_path(need)))
        if view is None:
            continue
        output = view.output
        tail = ""
        full = ""
        if isinstance(output, str):
            tail = "\n".join(output.splitlines()[-15:])
            full = output
        elif output is not None:
            full = json.dumps(output, ensure_ascii=False, indent=2, default=str)
            tail = full[:2000]
        needs.append(
            ApprovalNeed(
                path=str(scope.record_path(need)),
                status=view.status_name,
                duration_ms=rec.duration_ms if rec is not None else None,
                cost_usd=rec.cost_usd if rec is not None else None,
                tail=tail,
                output=full[:_VIEW_CAP],
                cost_source=rec.cost_source if rec is not None else "none",
            )
        )
    return needs


async def run_approve(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, attempt: int
) -> StepOutcome:
    """Run one approval gate (see module docstring)."""
    assert isinstance(step, ApproveStep)
    spec = step.approve
    path = record.path
    try:
        message = ctx.engine.render_str(spec.message, ctx.template_context(scope))
    except TemplateRenderError as exc:
        record.attempts += 1
        record.status = StepStatus.FAILED
        record.ok = False
        record.error = error_info(exc, type_="render")
        return StepOutcome(record=record)

    answer: ApprovalAnswer | None = None
    by = "cli"
    decision = stored_decision(ctx, path, record.attempts)
    if decision is not None:
        answer = ApprovalAnswer(decision.approved, decision.comment)
        by = decision.by
    else:
        record.attempts += 1
        if ctx.options.yes or ctx.options.dry_run:
            answer = ApprovalAnswer(True, "")
            by = "--yes" if ctx.options.yes else "dry-run"
        else:
            async with ctx.runtime.approval_lock:  # one gate at a time
                answer = await _ask(step, scope, ctx, record, message)
                if answer is None:
                    return await _pause(ctx, record, message)
            by = "tty"
    if ctx.run.pause is not None and ctx.run.pause.step == path:
        # this gate owned the pause slot: any decision (stored, --yes, dry-run, TTY) clears it
        ctx.run.pause = None
    await ctx.emit(
        EventType.RUN_DECISION,
        step_path=path,
        approved=answer.approved,
        comment=answer.comment,
        by=by,
    )
    record.approved = answer.approved
    outcome = StepOutcome(record=record, output=answer.comment or "", output_kind="text")
    if answer.approved:
        record.status = StepStatus.SUCCEEDED
        record.ok = True
        return outcome
    reason = f"rejected at {path}" + (f": {answer.comment}" if answer.comment else "")
    if spec.on_reject == "continue":
        record.status = StepStatus.SUCCEEDED
        record.ok = True
        return outcome
    if spec.on_reject == "fail":
        record.status = StepStatus.FAILED
        record.ok = False
        record.error = ErrorInfo(type="rejected", message=reason, transient=False)
        return outcome
    record.status = StepStatus.REJECTED
    record.ok = False
    outcome.control = RunStopped("cancelled", reason, step_path=path)
    return outcome


async def _ask(
    step: ApproveStep, scope: ExecScope, ctx: RunContext, record: StepRecord, message: str
) -> ApprovalAnswer | None:
    """Quiesce, then ask the injected prompt (TTY) or return ``None`` to pause."""
    rt = ctx.runtime
    rt.close_gate()
    try:
        await rt.wait_quiesced()
        prompt = ctx.approval_prompt
        if prompt is None or not ctx.options.interactive:
            return None
        request = ApprovalRequest(
            run_id=ctx.run.run_id,
            step_path=record.path,
            message=message,
            attempt=record.attempts,
            workdir=str(ctx.workdir),
            needs=_needs_summary(step, scope, ctx),
            totals={
                "steps": len(ctx.run.steps),
                "tokens": ctx.run.total_usage().total,
                "cost_usd": ctx.run.total_cost_usd(),
                "cost_source": run_cost_source(ctx),
            },
        )
        try:
            return await prompt(request)
        except anyio.get_cancelled_exc_class():
            # Ctrl-C at the prompt = pause: record it before the cancellation unwinds the run
            with anyio.CancelScope(shield=True):
                await _pause(ctx, record, message)
            raise
    finally:
        rt.open_gate()


async def _pause(ctx: RunContext, record: StepRecord, message: str) -> StepOutcome:
    """Mark the gate ``paused``; the FIRST pause of a run owns ``run.pause`` + ``run.paused``."""
    token = gate_token(record.path, record.attempts)
    record.status = StepStatus.PAUSED
    record.ok = None
    if ctx.paused is not None:
        # the run is already pausing at another gate: this gate pauses too (it asks again on
        # resume) but the decision slot stays with the first one
        await ctx.save_record(record)
        return StepOutcome(record=record, control=ctx.paused)
    control = RunPaused(token, record.path, message)
    ctx.paused = control
    ctx.run.pause = PauseInfo(token=token, step=record.path, message=message)
    await ctx.save_record(record)
    await ctx.emit(
        EventType.RUN_PAUSED, step_path=record.path, token=token, step=record.path, message=message
    )
    return StepOutcome(record=record, control=control)


__all__ = ["gate_token", "run_approve", "run_cost_source", "stored_decision"]
