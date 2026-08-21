# SPDX-License-Identifier: Apache-2.0
"""The prompt executor persists the rendered prompt: ``steps/<path>/prompt.txt``.

Write-ahead, beside the output and through ``FileRunStore.write_prompt`` — never a bare
``open()`` — so ``rayspec explain --full`` shows the bytes the agent actually received.
"""

from __future__ import annotations

from typing import Any

import pytest

from rayspec.engine.scheduler import run_graph
from rayspec.events.model import EventType
from rayspec.providers.stub import StubProvider

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


async def run_wf(harness: Harness, text: str, script: dict[str, Any] | None = None):
    harness.workflow("t", text)
    stub = StubProvider(script=script or {})
    g = make_graph_harness(harness, harness.load("t"), fake_leaf=False, providers={"claude": stub})
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    return g, outcomes


WF = """rayspec: 1
name: t
agents:
  reviewer: {provider: claude, model: small}
steps:
  - id: a
    agent: reviewer
    prompt: "Review {{ run.workflow }} please"
"""


async def test_rendered_prompt_is_persisted_and_referenced_by_the_record(
    harness: Harness,
) -> None:
    g, out = await run_wf(harness, WF, {"steps": {"a": {"text": "LGTM"}}})
    rec = out["a"].record
    assert rec.prompt_ref == "steps/a/prompt.txt"
    assert harness.store.read_output(g.run.run_id, rec.prompt_ref) == "Review t please"
    # the record that points at it is on disk too
    assert harness.store.load(g.run.run_id).steps["a"].prompt_ref == rec.prompt_ref


async def test_prompt_of_a_failed_attempt_is_persisted_too(harness: Harness) -> None:
    g, out = await run_wf(
        harness, WF, {"steps": {"a": {"fail": {"kind": "api", "message": "boom"}}}}
    )
    rec = out["a"].record
    assert rec.status.value == "failed"
    assert rec.prompt_ref == "steps/a/prompt.txt"
    assert harness.store.read_output(g.run.run_id, rec.prompt_ref) == "Review t please"


async def test_a_store_without_write_prompt_still_runs_the_step(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older/in-memory RunStore simply has no prompt copy — that is not a run problem."""
    monkeypatch.setattr(harness.store, "write_prompt", None, raising=False)
    _g, out = await run_wf(harness, WF, {"steps": {"a": {"text": "LGTM"}}})
    rec = out["a"].record
    assert rec.status.value == "succeeded" and rec.prompt_ref is None
    assert harness.events(EventType.WARNING) == []


async def test_a_failed_prompt_write_is_warned_about_where_the_user_can_see_it(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent degradation would let `explain` present a re-render as the persisted bytes."""

    def boom(*_args: Any, **_kw: Any) -> str:
        raise OSError("no space left on device")

    monkeypatch.setattr(harness.store, "write_prompt", boom)
    g, out = await run_wf(harness, WF, {"steps": {"a": {"text": "LGTM"}}})
    rec = out["a"].record
    assert rec.status.value == "succeeded" and rec.prompt_ref is None
    warnings = harness.events(EventType.WARNING)
    assert any("no space left on device" in (e.data.get("message", "")) for e in warnings), warnings
    assert all(e.step_path == "a" for e in warnings)
    # recorded, not just observed: it is in events.jsonl
    stored = [e for e in harness.store.read_events(g.run.run_id) if e.type is EventType.WARNING]
    assert any(
        "could not persist the rendered prompt" in (e.data.get("message", "")) for e in stored
    )
