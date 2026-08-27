# SPDX-License-Identifier: Apache-2.0
"""``loop:`` executor — do-while over a body graph.

Each iteration ``i`` (1-based) runs the body as its own graph in a child scope
``<loop>[i]`` with ``iteration = {n, max, first, prev}`` (``prev`` = the previous iteration's
step views, or absent on the first iteration). ``until`` is evaluated after every iteration
over that iteration's outputs; exhaustion with ``until`` still false ⇒ ``on_exhausted: fail``
(default) → failed, ``continue`` → succeeded with ``converged: false``. Output:
``{<body_id>: output}`` of the last executed iteration plus ``iterations`` / ``converged``.
A failed body step fails the loop (composite failure bubbles); control signals propagate.
"""

from __future__ import annotations

from typing import Any

from rayspec.engine.context import (
    BUDGET_SKIP_REASON,
    ExecScope,
    RunContext,
    StepOutcome,
    error_info,
)
from rayspec.engine.graph import FAILED_LIKE, StepGraph
from rayspec.events.model import EventType
from rayspec.schema import LoopStep, StepModel, StepStatus
from rayspec.store.model import ErrorInfo, LoopInfo, StepRecord
from rayspec.templating import TemplateRenderError


def body_outputs(outcomes: dict[str, StepOutcome], ids: tuple[str, ...]) -> dict[str, Any]:
    """``{<body_id>: output}`` (``None`` for steps that did not produce one)."""
    return {sid: (outcomes[sid].output if sid in outcomes else None) for sid in ids}


def failed_body_step(outcomes: dict[str, StepOutcome]) -> StepOutcome | None:
    """The first body outcome that fails the composite: an untolerated failed-like record, a
    paused gate, or a step skipped because the run-level cap tripped."""
    for outcome in outcomes.values():
        rec = outcome.record
        if rec.status in FAILED_LIKE and not rec.tolerated:
            return outcome
        if rec.status is StepStatus.PAUSED:
            return outcome
        if rec.status is StepStatus.SKIPPED and rec.skip_reason == BUDGET_SKIP_REASON:
            return outcome
    return None


def body_failure_message(outcome: StepOutcome) -> str:
    """``<error message>`` | ``<skip reason>`` | ``<status>`` of a failing body outcome."""
    rec = outcome.record
    if rec.error is not None:
        return rec.error.message
    if rec.status is StepStatus.SKIPPED and rec.skip_reason:
        return rec.skip_reason.replace("_", " ")
    return rec.status.value


async def run_loop(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, attempt: int
) -> StepOutcome:
    """Run a ``loop:`` step (see module docstring)."""
    assert isinstance(step, LoopStep)
    from rayspec.engine.scheduler import run_graph

    spec = step.loop
    graph = StepGraph.from_steps(spec.steps)
    n_max = spec.max_iterations
    prev_views: dict[str, Any] | None = None
    last: dict[str, StepOutcome] = {}
    iterations = 0
    converged: bool | None = None
    error: ErrorInfo | None = None
    for n in range(1, n_max + 1):
        if ctx.budget_exceeded is not None:
            # no new iteration once the run-level cap tripped
            error = ErrorInfo(
                type="budget",
                message=f"{ctx.budget_exceeded}: no further iteration started",
                transient=False,
            )
            break
        cancelled = await ctx.check_cancelled()
        if cancelled is not None:
            # PRD-07 R5: the iteration already in flight (if any) already finished before this
            # loop got back here — only the NEXT one is refused
            error = ErrorInfo(
                type="cancelled",
                message=f"{cancelled}: no further iteration started",
                transient=False,
            )
            break
        iteration = {"n": n, "max": n_max, "first": n == 1, "prev": prev_views}
        child = scope.child(
            prefix=scope.record_path(step.id).indexed(n),
            def_prefix=f"{scope.def_path(step.id)}/",
            variables={"iteration": iteration},
            iteration=n,
        )
        await ctx.emit(EventType.LOOP_ITERATION, step_path=record.path, n=n, max=n_max)
        outcomes = await run_graph(graph, child, ctx)
        iterations = n
        last = outcomes
        prev_views = dict(child.views)
        failed = failed_body_step(outcomes)
        if failed is not None:
            msg = body_failure_message(failed)
            status = failed.record.status.value
            error = ErrorInfo(
                type="body",
                message=f"iteration {n}: step {failed.record.id!r} {status}: {msg}",
                transient=False,
            )
            break
        if spec.until is not None:
            try:
                done = ctx.engine.eval_bool(spec.until, ctx.template_context(child))
            except TemplateRenderError as exc:
                error = error_info(exc, type_="until")
                break
            if done:
                converged = True
                break
    record.attempts = max(record.attempts, 1)
    if error is None and spec.until is not None and converged is not True:
        converged = False
        if spec.on_exhausted == "fail":
            error = ErrorInfo(
                type="exhausted",
                message=(
                    f"loop exhausted after {iterations} iteration(s) without satisfying "
                    f"until: {spec.until}"
                ),
                transient=False,
            )
    if error is None and spec.until is None:
        converged = True
    record.loop = LoopInfo(iterations=iterations, converged=converged)
    output = body_outputs(last, graph.ids)
    if error is not None:
        record.status = StepStatus.FAILED
        record.ok = False
        record.error = error
        return StepOutcome(record=record, output=output if last else None, output_kind="json")
    record.status = StepStatus.SUCCEEDED
    record.ok = True
    return StepOutcome(record=record, output=output, output_kind="json")


__all__ = ["body_failure_message", "body_outputs", "failed_body_step", "run_loop"]
