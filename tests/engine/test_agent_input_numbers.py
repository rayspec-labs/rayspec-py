# SPDX-License-Identifier: Apache-2.0
"""E1 (PRD-09 F6/F13): the engine resolves an agent's `{{ inputs.<name> }}` budget_usd/max_turns
to a concrete number once per run entry (after _prepare_record), so the provider request carries
the input's value and a resume uses the inputs the record already fixed."""

from __future__ import annotations

from typing import Any

import pytest

from rayspec.providers.base import AgentRequest, AgentResult, EmitFn
from rayspec.providers.stub import StubProvider
from rayspec.schema import RunStatus

from .conftest import Harness

pytestmark = pytest.mark.anyio

WF = """
rayspec: 1
name: t
inputs:
  b: {type: number, default: 30}
  t: {type: integer, default: 5}
agents:
  impl: {provider: stub, budget_usd: "{{ inputs.b }}", max_turns: "{{ inputs.t }}"}
steps:
  - {id: a, agent: impl, prompt: hi}
"""


class RecordingStub(StubProvider):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.requests: list[AgentRequest] = []

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        self.requests.append(req)
        return await super().run(req, emit)


async def test_the_request_carries_the_resolved_input_backed_numbers(harness: Harness) -> None:
    harness.workflow("t", WF)
    stub = RecordingStub()
    result = await harness.run("t", inputs={"b": 42.5, "t": 8}, providers={"stub": stub})
    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert stub.requests, "the provider was never asked"
    assert stub.requests[0].budget_usd == 42.5
    assert stub.requests[0].max_turns == 8


async def test_a_missing_referenced_input_is_an_engine_error(harness: Harness) -> None:
    """An input reference to a name the run cannot supply fails the run entry (exit 2), not a
    crash mid-run — resolve_agent_numbers raises LoaderError, mapped to EngineError."""
    wf = """
rayspec: 1
name: t
inputs:
  other: {type: integer, default: 1}
agents:
  impl: {provider: stub, max_turns: "{{ inputs.other }}"}
steps:
  - {id: a, agent: impl, prompt: hi}
"""
    # `other` exists, so this validates & runs; force a missing input by resolving directly
    from rayspec.errors import LoaderError
    from rayspec.loader.agent_numbers import resolve_agent_numbers

    harness.workflow("t", wf)
    rw = harness.load("t")
    with pytest.raises(LoaderError) as exc:
        resolve_agent_numbers(rw, {})  # the run somehow has no `other`
    assert "other" in str(exc.value)


async def test_a_resume_reuses_the_prompt_step_without_re_asking(harness: Harness) -> None:
    """Resolving is per-entry (not recorded); a resume of a finished run replays the cached
    prompt step rather than re-resolving and re-asking the provider."""
    harness.workflow("t", WF)
    stub = RecordingStub()
    first = await harness.run("t", inputs={"b": 10, "t": 3}, providers={"stub": stub})
    assert first.status is RunStatus.SUCCEEDED
    asked_once = len(stub.requests)
    second = await harness.run("t", resume=first.run_id, providers={"stub": stub})
    assert second.status is RunStatus.SUCCEEDED
    # the finished step was replayed from cache, not re-asked
    assert len(stub.requests) == asked_once
