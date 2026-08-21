"""An interrupted / timed-out prompt attempt records the usage the adapter reported so far
(the last ``usage`` stream event's ``turn_total``), counts it in the step and run totals across
resumes, and marks the attempt's usage unknown (never zero) when nothing was reported."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from rayspec.engine.scheduler import run_leaf
from rayspec.events.model import EventType
from rayspec.providers.base import (
    AgentEvent,
    AgentRequest,
    AgentResult,
    EmitFn,
    ProviderError,
    Usage,
)
from rayspec.providers.stub import StubProvider
from rayspec.schema import RunStatus, StepStatus

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


class HangingProvider(StubProvider):
    """Emits scripted ``usage`` events, then hangs until :attr:`answer` is set (then answers
    like the stub)."""

    def __init__(
        self,
        usage_events: list[dict[str, int]],
        *,
        answer: bool = False,
        raise_after: bool = False,
    ) -> None:
        super().__init__(script={"defaults": {"usage": {"input": 7, "output": 3}}})
        self.usage_events = usage_events
        self.answer = answer
        self.raise_after = raise_after

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        total = Usage()
        for raw in self.usage_events:
            delta = Usage(**raw)
            total = total + delta
            await emit(
                AgentEvent(
                    kind="usage",
                    data={
                        "usage": {"input": delta.input, "output": delta.output},
                        "turn_total": {
                            "input": total.input,
                            "cached_input": total.cached_input,
                            "cache_write": total.cache_write,
                            "output": total.output,
                            "reasoning": total.reasoning,
                        },
                    },
                )
            )
        if self.raise_after:
            self.raise_after = False  # raise once, answer on the retry
            raise ProviderError("boom after usage", transient=True, kind="api")
        if self.answer:
            return await super().run(req, emit)
        await anyio.sleep_forever()
        raise AssertionError("unreachable")


def _wf(extra: str = "") -> str:
    return f"""
rayspec: 1
name: t
steps:
  - {{id: a, shell: echo a}}
  - {{id: think, needs: [a], prompt: "think", agent: {{provider: claude}}{extra}}}
"""


async def test_timed_out_attempt_records_the_partial_usage(harness: Harness) -> None:
    harness.workflow("t", _wf(", timeout: 0.3, retry: {attempts: 1}"))
    provider = HangingProvider([{"input": 100, "output": 10}, {"input": 50, "output": 5}])
    result = await harness.run("t", providers={"claude": provider})
    assert result.status is RunStatus.FAILED
    rec = result.steps["think"]
    assert rec.status is StepStatus.FAILED and rec.error and rec.error.type == "timeout"
    assert rec.usage == Usage(input=150, output=15) and rec.usage_unknown is False
    assert result.usage.total == 165
    assert "usage_unknown" not in harness.finished("think").data


async def test_timed_out_attempt_without_any_usage_report_is_unknown_not_zero(
    harness: Harness,
) -> None:
    harness.workflow("t", _wf(", timeout: 0.3, retry: {attempts: 1}"))
    result = await harness.run("t", providers={"claude": HangingProvider([])})
    rec = result.steps["think"]
    assert rec.status is StepStatus.FAILED and rec.usage.total == 0
    assert rec.usage_unknown is True
    assert harness.finished("think").data["usage_unknown"] is True
    assert harness.record(result.run_id).steps["think"].usage_unknown is True


async def test_interrupted_attempt_usage_counts_in_the_totals_after_resume(
    harness: Harness,
) -> None:
    harness.workflow("t", _wf())
    provider = HangingProvider([{"input": 200, "output": 20}])
    runner = harness.runner("t", run_id="20260820-000000-usg", providers={"claude": provider})
    with anyio.move_on_after(0.8):
        await runner.run()
    run = harness.record("20260820-000000-usg")
    assert run.status is RunStatus.INTERRUPTED
    think = run.steps["think"]
    assert think.status is StepStatus.INTERRUPTED
    assert think.usage == Usage(input=200, output=20) and think.usage_unknown is False
    assert run.total_usage().total == 220
    interrupted = harness.finished("think")
    assert interrupted.data["status"] == "interrupted"
    assert interrupted.data["usage"]["input"] == 200

    # resume: attempt 2 answers; the step and run totals include the interrupted attempt
    harness.sink.clear()
    provider.answer = True
    result = await harness.run("t", resume="20260820-000000-usg", providers={"claude": provider})
    assert result.status is RunStatus.SUCCEEDED
    rec = result.steps["think"]
    assert rec.attempts == 2 and rec.status is StepStatus.SUCCEEDED
    assert rec.usage == Usage(input=200 + 7, output=20 + 3) and rec.usage_unknown is False
    assert result.usage.total == 230
    assert harness.events(EventType.RUN_FINISHED)[-1].data["usage"]["input"] == 207


async def test_interrupted_attempt_without_usage_stays_unknown_after_resume(
    harness: Harness,
) -> None:
    harness.workflow("t", _wf())
    provider = HangingProvider([])
    runner = harness.runner("t", run_id="20260820-000000-unk", providers={"claude": provider})
    with anyio.move_on_after(0.8):
        await runner.run()
    run = harness.record("20260820-000000-unk")
    assert run.steps["think"].usage_unknown is True and run.steps["think"].usage.total == 0
    harness.sink.clear()
    provider.answer = True
    result = await harness.run("t", resume="20260820-000000-unk", providers={"claude": provider})
    rec = result.steps["think"]
    # attempt 2 reported usage; attempt 1 remains unknown, so the step total is a lower bound
    assert rec.status is StepStatus.SUCCEEDED and rec.usage == Usage(input=7, output=3)
    assert rec.usage_unknown is True
    assert harness.finished("think").data["usage_unknown"] is True


async def test_provider_timeout_status_without_usage_is_unknown(harness: Harness) -> None:
    # the stub's own ``status: timeout`` (latency above the step timeout) reports no usage
    harness.workflow("t", _wf(", timeout: 0.4, retry: {attempts: 1}"))
    stub = StubProvider(script={"steps": {"think": {"latency_ms": 5000}}})
    result = await harness.run("t", providers={"claude": stub})
    rec = result.steps["think"]
    assert rec.status is StepStatus.FAILED and rec.error and rec.error.type == "timeout"
    assert rec.usage_unknown is True


def test_step_record_usage_unknown_defaults_false_for_old_records() -> None:
    from rayspec.store.model import StepRecord

    rec: Any = StepRecord.model_validate({"path": "x", "id": "x", "kind": "prompt"})
    assert rec.usage_unknown is False


async def test_cancel_while_waiting_for_the_permit_keeps_the_carried_usage(
    harness: Harness,
) -> None:
    # a leaf queued behind ``max_parallel`` (or the launch gate) that is
    # cancelled before its attempt starts must keep the usage/cost its record carried over
    # (from earlier attempts / the previous run) — the per-attempt reset happens only once the
    # attempt actually runs, and an attempt that never started is not counted.
    harness.workflow(
        "t",
        """
        rayspec: 1
        name: t
        defaults: {max_parallel: 1}
        steps:
          - {id: hog, shell: hang}
          - {id: think, shell: ok}
        """,
    )
    gh = make_graph_harness(harness, harness.load("t"))
    think = gh.graph.step("think")
    record = gh.ctx.new_record(think, gh.scope)
    record.attempts = 1
    record.usage = Usage(input=100, output=10)
    record.cost_usd = 0.5
    record.cost_source = "table"

    async def hold_the_only_permit() -> None:
        async with gh.ctx.runtime.leaf_permit():
            await anyio.sleep_forever()

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold_the_only_permit)
        await anyio.sleep(0.05)
        tg.start_soon(run_leaf, think, gh.scope, gh.ctx, record, gh.leaf)
        await anyio.sleep(0.1)
        tg.cancel_scope.cancel()

    assert gh.leaf.calls == {}  # the attempt never started
    assert record.usage == Usage(input=100, output=10) and record.usage_unknown is False
    assert record.cost_usd == 0.5 and record.cost_source == "table"
    assert record.attempts == 1


async def test_provider_error_before_any_usage_report_is_not_unknown(harness: Harness) -> None:
    # a ProviderError raised before a token was billed (auth failure, CLI
    # missing, a 429 raised by the SDK) is a plain failed attempt with zero usage — the
    # ``usage_unknown`` flag is reserved for attempts cut off mid-flight.
    harness.workflow("t", _wf(", retry: {attempts: 2, delay: 0}"))
    stub = StubProvider(
        script={
            "defaults": {"usage": {"input": 7, "output": 3}},
            "steps": {
                "think": {
                    "text": "ok",
                    "fail": {"kind": "api", "transient": True, "times": 1, "raise": True},
                }
            },
        }
    )
    result = await harness.run("t", providers={"claude": stub})
    rec = result.steps["think"]
    assert rec.status is StepStatus.SUCCEEDED and rec.attempts == 2
    assert rec.usage == Usage(input=7, output=3)
    assert rec.usage_unknown is False
    assert "usage_unknown" not in harness.finished("think").data
    assert result.usage.total == 10


async def test_provider_error_after_a_usage_report_keeps_the_partial_usage(
    harness: Harness,
) -> None:
    harness.workflow("t", _wf(", retry: {attempts: 2, delay: 0}"))
    provider = HangingProvider([{"input": 100, "output": 10}], answer=True, raise_after=True)
    result = await harness.run("t", providers={"claude": provider})
    rec = result.steps["think"]
    assert rec.status is StepStatus.SUCCEEDED and rec.attempts == 2
    # attempt 1: the 100/10 reported before the error; attempt 2: the stub's result usage 7/3
    assert rec.usage == Usage(input=107, output=13)
    assert rec.usage_unknown is False
