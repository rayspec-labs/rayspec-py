# SPDX-License-Identifier: Apache-2.0
"""``each:`` executor — dynamic fan-out over a sequence.

The ``each`` expression must evaluate to a list/tuple (anything else ⇒ failed); an empty
sequence ⇒ succeeded with output ``[]``. Items run concurrently under an extra
``CapacityLimiter(each.max_parallel)`` (the leaf limiter still applies), each in a child scope
``<each>[i]`` (0-based) with ``each = {index, total}`` and the item bound to ``as:``. Every
record of an item's graph carries ``item_index`` / ``item_sha256`` (resume: a changed item is
re-run). ``on_failure: continue`` tolerates item failures (``None`` slots in the output,
``items`` detail records the error); the default ``fail`` fails the step once every item
has finished. Control signals (``stop:``, a rejected or paused gate) raised by one or more
items concurrently collapse into ONE signal: the first one wins (a pause beats a stop), the
other items are cancelled (``interrupted`` with reason ``stopped``/``paused``) and the
signal bubbles after every item has wound down — never a multi-leaf ``ExceptionGroup``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AsyncExitStack
from typing import Any

import anyio
from anyio.abc import TaskGroup

from rayspec.engine.context import ExecScope, RunContext, StepOutcome, error_info, sha256_json
from rayspec.engine.errors import RunControl, RunPaused
from rayspec.engine.executors.loop import body_failure_message, body_outputs, failed_body_step
from rayspec.engine.graph import StepGraph
from rayspec.engine.runtime import unwrap_exception_group
from rayspec.events.model import EventType
from rayspec.schema import EachStep, StepModel, StepStatus
from rayspec.store.model import EachInfo, ErrorInfo, StepRecord
from rayspec.templating import TemplateRenderError


def _as_sequence(value: Any) -> list[Any] | None:
    """``list(value)`` for any iterable except text/bytes/mappings (``.values()``/``.items()``
    views, ``range``, generators are fine); ``None`` when it is not a sequence."""
    if isinstance(value, str | bytes | Mapping):
        return None
    if isinstance(value, list | tuple):
        return list(value)
    if isinstance(value, Iterable):
        try:
            return list(value)
        except Exception:
            return None
    return None


async def run_each(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, attempt: int
) -> StepOutcome:
    """Run an ``each:`` step (see module docstring)."""
    assert isinstance(step, EachStep)
    from rayspec.engine.scheduler import cancel_reason_for, run_graph

    tctx = ctx.template_context(scope)
    try:
        raw = ctx.engine.eval_expr(step.each, tctx)
    except TemplateRenderError as exc:
        return _fail(record, error_info(exc, type_="each"))
    items = _as_sequence(raw)
    if items is None:
        return _fail(
            record,
            ErrorInfo(
                type="each",
                message=(
                    f"each: {step.each!r} must evaluate to a list, got {type(raw).__name__}"
                    + (" (use | fromjson for JSON text, or .values()/.items() for mappings)")
                ),
                transient=False,
            ),
        )
    total = len(items)
    graph = StepGraph.from_steps(step.steps)
    results: list[Any] = [None] * total
    details: list[dict[str, Any]] = [
        {"index": i, "item": item, "status": "pending", "output": None, "error": None}
        for i, item in enumerate(items)
    ]
    if total == 0:
        record.status = StepStatus.SUCCEEDED
        record.ok = True
        record.each = EachInfo(total=0, succeeded=0, failed=0)
        return StepOutcome(record=record, output=[], output_kind="json", items=details)

    limiter = anyio.CapacityLimiter(step.max_parallel) if step.max_parallel else None
    children: list[ExecScope] = []
    control: list[RunControl] = []  # the one signal that bubbles (slot 0)

    def signal(exc: RunControl, tg: TaskGroup) -> None:
        # first signal wins, except that a pause beats a stop (the run stays resumable; the
        # runner also ranks ``paused`` above ``stopped``)
        if control and not (isinstance(exc, RunPaused) and not isinstance(control[0], RunPaused)):
            return
        control[:] = [exc]
        reason = cancel_reason_for(exc)
        for child in children:
            if child.cancel_reason is None:
                child.cancel_reason = reason
        tg.cancel_scope.cancel()

    async def run_item(index: int, item: Any, tg: TaskGroup) -> None:
        child = scope.child(
            prefix=scope.record_path(step.id).indexed(index),
            def_prefix=f"{scope.def_path(step.id)}/",
            variables={"each": {"index": index, "total": total}, step.as_: item},
            item_index=index,
            item_sha256=sha256_json(item),
        )
        children.append(child)
        await ctx.emit(EventType.EACH_ITEM, step_path=record.path, index=index, total=total)
        try:
            async with AsyncExitStack() as stack:
                if limiter is not None:
                    await stack.enter_async_context(limiter)
                outcomes = await run_graph(graph, child, ctx)
        except RunControl as exc:
            signal(exc, tg)
            return
        failed = failed_body_step(outcomes)
        detail = details[index]
        if failed is not None:
            rec = failed.record
            detail["status"] = "failed"
            detail["error"] = f"{rec.id}: {body_failure_message(failed)}"
            results[index] = None
        else:
            detail["status"] = "succeeded"
            detail["output"] = body_outputs(outcomes, graph.ids)
            results[index] = detail["output"]

    try:
        async with anyio.create_task_group() as tg:
            for index, item in enumerate(items):
                tg.start_soon(run_item, index, item, tg)
    except BaseExceptionGroup as group:  # a bug in an item: surface it as one failed step
        inner = unwrap_exception_group(group)
        if isinstance(inner, Exception) and not isinstance(inner, BaseExceptionGroup):
            raise inner from None
        raise
    if control:
        raise control[0]
    failed_count = sum(1 for d in details if d["status"] == "failed")
    record.each = EachInfo(total=total, succeeded=total - failed_count, failed=failed_count)
    record.attempts = max(record.attempts, 1)
    if failed_count and step.on_failure == "fail":
        record.status = StepStatus.FAILED
        record.ok = False
        record.error = ErrorInfo(
            type="items",
            message=f"{failed_count} of {total} item(s) failed: "
            + "; ".join(str(d["error"]) for d in details if d["status"] == "failed")[:500],
            transient=False,
        )
        return StepOutcome(record=record, output=results, output_kind="json", items=details)
    record.status = StepStatus.SUCCEEDED
    record.ok = True
    return StepOutcome(record=record, output=results, output_kind="json", items=details)


def _fail(record: StepRecord, error: ErrorInfo) -> StepOutcome:
    record.status = StepStatus.FAILED
    record.ok = False
    record.error = error
    record.attempts = max(record.attempts, 1)
    return StepOutcome(record=record)


__all__ = ["run_each"]
