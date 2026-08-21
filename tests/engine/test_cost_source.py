"""Run-level ``cost_source``: ``provider`` / ``table`` / ``partial`` / ``none`` computed
by the engine's totals aggregation and persisted on ``run.json`` (``RunRecord.cost_source``),
the ``run.finished`` event and the :class:`RunResult`."""

from __future__ import annotations

from typing import Any

import pytest

from rayspec.engine.context import RunOptions, cost_source_of, totals_of
from rayspec.events.model import EventType
from rayspec.providers.base import AgentRequest, AgentResult, EmitFn, Usage
from rayspec.providers.pricing import PriceTable
from rayspec.providers.stub import StubProvider
from rayspec.schema import RunStatus
from rayspec.store.model import StepRecord

from .conftest import Harness

pytestmark = pytest.mark.anyio


def _rec(
    path: str, *, tokens: int = 0, cost: float | None = None, source: str = "none"
) -> StepRecord:
    return StepRecord(
        path=path,
        id=path,
        kind="prompt",
        usage=Usage(input=tokens, output=0),
        cost_usd=cost,
        cost_source=source,
    )


def test_cost_source_of_the_four_cases() -> None:
    assert cost_source_of([]) == "none"
    assert cost_source_of([_rec("a", tokens=10)]) == "none"  # tokens, no cost anywhere
    assert cost_source_of([_rec("a", tokens=10, cost=0.1, source="provider")]) == "provider"
    assert (
        cost_source_of(
            [
                _rec("a", tokens=10, cost=0.1, source="provider"),
                _rec("b", tokens=10, cost=0.2, source="table"),
            ]
        )
        == "table"
    )
    # a step with tokens but no cost at all: the total is a lower bound
    assert (
        cost_source_of([_rec("a", tokens=10, cost=0.1, source="provider"), _rec("b", tokens=10)])
        == "partial"
    )
    assert (
        cost_source_of([_rec("a", tokens=10, cost=0.1, source="table"), _rec("b", tokens=10)])
        == "partial"
    )
    # a shell step (no tokens, no cost) never makes a run partial
    shell = StepRecord(path="s", id="s", kind="shell")
    assert cost_source_of([_rec("a", tokens=10, cost=0.1, source="provider"), shell]) == "provider"
    usage, cost, source = totals_of([_rec("a", tokens=10, cost=0.1, source="provider"), shell])
    assert usage.total == 10 and cost == pytest.approx(0.1) and source == "provider"
    assert totals_of([]) == (Usage(), None, "none")


class PricedStub(StubProvider):
    """A stub whose results carry a provider-reported cost (like Claude)."""

    def __init__(self, cost: float, **kw: Any) -> None:
        super().__init__(**kw)
        self.cost = cost

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        result = await super().run(req, emit)
        result.cost_usd = self.cost
        result.cost_source = "provider"
        return result


WF = """
rayspec: 1
name: mixed
agents:
  a_agent: {provider: claude, model: m1}
  b_agent: {provider: codex, model: m2}
steps:
  - {id: a, agent: a_agent, prompt: one}
  - {id: b, needs: [a], agent: b_agent, prompt: two}
"""


async def _mixed(harness: Harness, *, b: StubProvider, price_table: PriceTable | None = None):
    harness.workflow("mixed", WF)
    runner = harness.runner(
        "mixed",
        providers={
            "claude": PricedStub(0.02, script={"defaults": {"usage": {"input": 100, "output": 1}}}),
            "codex": b,
        },
    )
    runner.price_table = price_table
    return await runner.run()


async def test_mixed_priced_and_unpriced_steps_are_partial(harness: Harness) -> None:
    plain = StubProvider(script={"defaults": {"usage": {"input": 500, "output": 1}}})
    result = await _mixed(harness, b=plain)
    assert result.status is RunStatus.SUCCEEDED
    assert result.cost_usd == pytest.approx(0.02) and result.cost_source == "partial"
    run = harness.record(result.run_id)
    assert run.cost_source == "partial" and run.steps["b"].cost_source == "none"
    finished = harness.events(EventType.RUN_FINISHED)[-1]
    assert finished.data["cost_source"] == "partial"
    assert finished.data["cost_usd"] == pytest.approx(0.02)


async def test_table_estimate_for_the_unpriced_provider_makes_the_run_table(
    harness: Harness,
) -> None:
    plain = StubProvider(script={"defaults": {"usage": {"input": 1000, "output": 0}}})
    table = PriceTable.from_config({"m2": {"input": 2.0, "cached_input": 0, "output": 0}})
    result = await _mixed(harness, b=plain, price_table=table)
    assert result.cost_source == "table"
    assert result.cost_usd == pytest.approx(0.02 + 0.002)
    assert harness.record(result.run_id).cost_source == "table"


async def test_every_step_priced_by_the_provider_is_provider(harness: Harness) -> None:
    result = await _mixed(harness, b=PricedStub(0.01, script={}))
    assert result.cost_source == "provider" and result.cost_usd == pytest.approx(0.03)
    assert harness.record(result.run_id).cost_source == "provider"


async def test_no_cost_anywhere_is_none(harness: Harness) -> None:
    harness.workflow("mixed", WF)
    result = await harness.run("mixed", options=RunOptions(dry_run=True))
    assert result.status is RunStatus.SUCCEEDED and result.usage.total > 0
    assert result.cost_usd is None and result.cost_source == "none"
    run = harness.record(result.run_id)
    assert run.cost_source == "none"
    assert "cost_source" not in harness.events(EventType.RUN_FINISHED)[-1].data
