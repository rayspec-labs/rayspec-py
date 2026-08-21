# SPDX-License-Identifier: Apache-2.0
"""``include:`` executor — runs an included workflow's body as a lexically scoped graph.

``with:`` is deep-rendered, coerced/defaulted against the included workflow's ``inputs:`` and
validated; the body runs in a fresh scope chain (inner steps are not addressable from outside
and the body does not see the including workflow's steps). The step output is the included
workflow's rendered ``outputs:`` map.
"""

from __future__ import annotations

from typing import Any

import jsonschema

from rayspec.engine.context import ExecScope, RunContext, StepOutcome, error_info
from rayspec.engine.executors.loop import body_failure_message, failed_body_step
from rayspec.engine.graph import StepGraph
from rayspec.loader.inputs import coerce_input
from rayspec.schema import IncludeStep, StepModel, StepStatus, inputs_to_json_schema
from rayspec.store.model import ErrorInfo, StepRecord
from rayspec.templating import TemplateRenderError


def resolve_include_inputs(
    body_inputs: dict[str, Any], with_values: dict[str, Any]
) -> dict[str, Any]:
    """Defaults + coercion + schema validation of ``with:`` against the included ``inputs:``."""
    values: dict[str, Any] = {}
    errors: list[str] = []
    for name, spec in body_inputs.items():
        if name in with_values:
            try:
                values[name] = coerce_input(with_values[name], spec, name=name)
            except Exception as exc:  # InputError
                errors.append(str(exc))
        elif spec.has_default:
            values[name] = spec.default
        elif spec.required:
            errors.append(f"missing required input {name!r}")
    unknown = sorted(set(with_values) - set(body_inputs))
    if unknown:
        errors.append(f"unknown input(s): {', '.join(unknown)}")
    if errors:
        raise ValueError("; ".join(errors))
    jsonschema.validate(values, inputs_to_json_schema(body_inputs))
    return values


async def run_include(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, attempt: int
) -> StepOutcome:
    """Run an ``include:`` step (see module docstring)."""
    assert isinstance(step, IncludeStep)
    from rayspec.engine.scheduler import run_graph

    record.attempts = max(record.attempts, 1)
    def_path = scope.def_path(step.id)
    body = ctx.resolved.includes.get(def_path)
    if body is None:
        return _fail(
            record, ErrorInfo(type="include", message=f"include body not loaded for {def_path}")
        )
    tctx = ctx.template_context(scope)
    try:
        with_values = ctx.engine.render_value(dict(step.with_), tctx)
        inputs = resolve_include_inputs(body.inputs, dict(with_values))
    except (TemplateRenderError, ValueError, jsonschema.ValidationError) as exc:
        return _fail(record, error_info(exc, type_="with"))
    child = scope.child(
        prefix=scope.record_path(step.id),
        def_prefix=f"{def_path}/",
        inputs=inputs,
        defaults=body.defaults,
        lexical_root=True,
    )
    graph = StepGraph.from_steps(body.steps)
    outcomes = await run_graph(graph, child, ctx)
    failed = failed_body_step(outcomes)
    if failed is not None:
        rec = failed.record
        msg = body_failure_message(failed)
        return _fail(
            record,
            ErrorInfo(
                type="body", message=f"step {rec.id!r} {rec.status.value}: {msg}", transient=False
            ),
        )
    try:
        outputs = ctx.engine.render_value(dict(body.outputs), ctx.template_context(child))
    except TemplateRenderError as exc:
        return _fail(record, error_info(exc, type_="outputs"))
    record.status = StepStatus.SUCCEEDED
    record.ok = True
    return StepOutcome(record=record, output=outputs, output_kind="json")


def _fail(record: StepRecord, error: ErrorInfo) -> StepOutcome:
    record.status = StepStatus.FAILED
    record.ok = False
    record.error = error
    return StepOutcome(record=record)


__all__ = ["resolve_include_inputs", "run_include"]
