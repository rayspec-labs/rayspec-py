# SPDX-License-Identifier: Apache-2.0
"""``steps.<id>.ok`` on a **skipped** step is undefined-with-hint, not a silent ``false``.

The guard rail this pins: ``when: steps.lint.ok`` on a step that was skipped must fail loudly
(with the same shape of hint ``.output`` already gives) instead of quietly reading as "not ok" —
a skipped step never answered the question. Every other status keeps its boolean.
"""

from __future__ import annotations

import json

import pytest
from jinja2 import UndefinedError

from rayspec.schema import StepStatus
from rayspec.templating import RayspecUndefined, Scope, StepView, TemplateEngine, build_context


def _view(status: StepStatus, **kwargs: object) -> StepView:
    return StepView(id="lint", kind="shell", status=status, **kwargs)  # type: ignore[arg-type]


def _ctx(view: StepView) -> dict[str, object]:
    return build_context(Scope(None, {view.id: view}), inputs={}, run={}, project={}, env={})


def test_ok_on_a_skipped_step_is_undefined_with_the_guard_hint() -> None:
    value = _view(StepStatus.SKIPPED, skip_reason="when_false").resolve("ok")
    assert isinstance(value, RayspecUndefined)
    with pytest.raises(UndefinedError) as exc:
        bool(value)
    message = str(exc.value)
    assert "step 'lint' was skipped (when_false)" in message
    assert "steps.lint.status == 'succeeded'" in message


def test_ok_hint_mirrors_the_output_hint() -> None:
    view = _view(StepStatus.SKIPPED, skip_reason="upstream_failed")
    ok = view.resolve("ok")
    output = view.resolve("output")
    assert isinstance(ok, RayspecUndefined) and isinstance(output, RayspecUndefined)
    assert ok.rayspec_hint == output.rayspec_hint


def test_ok_stays_a_boolean_for_every_other_status() -> None:
    assert _view(StepStatus.SUCCEEDED, ok=True).resolve("ok") is True
    assert _view(StepStatus.FAILED, ok=False).resolve("ok") is False
    assert _view(StepStatus.FAILED, ok=False, tolerated=True).resolve("ok") is False
    assert _view(StepStatus.PENDING).resolve("ok") is False


def test_when_on_a_skipped_ok_fails_loudly_instead_of_reading_false() -> None:
    engine = TemplateEngine()
    ctx = _ctx(_view(StepStatus.SKIPPED, skip_reason="when_false"))
    with pytest.raises(Exception) as exc:
        engine.eval_bool("steps.lint.ok", ctx)
    assert "was skipped" in str(exc.value)
    # the documented guard keeps working
    assert engine.eval_bool("steps.lint.status == 'succeeded'", ctx) is False


def test_default_and_is_defined_still_work_on_a_skipped_ok() -> None:
    engine = TemplateEngine()
    ctx = _ctx(_view(StepStatus.SKIPPED, skip_reason="when_false"))
    assert engine.eval_expr("steps.lint.ok | default(false)", ctx) is False
    assert engine.eval_bool("steps.lint.ok is defined", ctx) is False


def test_context_json_of_a_skipped_step_reports_ok_null() -> None:
    data = _view(StepStatus.SKIPPED, skip_reason="when_false").to_json()
    assert data["ok"] is None and data["status"] == "skipped"
    json.dumps(data)  # the RAYSPEC_CONTEXT dump must stay serialisable
