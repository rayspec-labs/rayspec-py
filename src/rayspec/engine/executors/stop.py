# SPDX-License-Identifier: Apache-2.0
"""``stop:`` executor: render the reason, succeed, and signal :class:`RunStopped`.

Module boundary: the step itself ends ``succeeded`` (its output is the rendered reason so the
record is complete); the scheduler turns the attached control into run-level behaviour.
"""

from __future__ import annotations

from rayspec.engine.context import ExecScope, RunContext, StepOutcome, error_info
from rayspec.engine.errors import RunStopped
from rayspec.schema import StepModel, StepStatus, StopStep
from rayspec.store.model import StepRecord
from rayspec.templating import TemplateRenderError


async def run_stop(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, attempt: int
) -> StepOutcome:
    """Render ``stop.reason`` and return a succeeded outcome carrying ``RunStopped``."""
    assert isinstance(step, StopStep)
    reason: str | None = None
    if step.stop.reason is not None:
        try:
            reason = ctx.engine.render_str(step.stop.reason, ctx.template_context(scope))
        except TemplateRenderError as exc:
            record.status = StepStatus.FAILED
            record.ok = False
            record.error = error_info(exc, type_="render")
            return StepOutcome(record=record)
    record.status = StepStatus.SUCCEEDED
    record.ok = True
    control = RunStopped(step.stop.status, reason, step_path=record.path)
    return StepOutcome(record=record, output=reason or "", output_kind="text", control=control)


__all__ = ["run_stop"]
