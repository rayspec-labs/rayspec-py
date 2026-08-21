"""approve: executor hands the prompt cost sources (the panel renders ``~$`` / ``—``)."""

from __future__ import annotations

import pytest

from rayspec.engine.approval import ApprovalAnswer, ApprovalRequest
from rayspec.providers.pricing import PriceTable
from rayspec.providers.stub import StubProvider
from rayspec.schema import RunStatus

from .conftest import Harness

pytestmark = pytest.mark.anyio

WF = """
rayspec: 1
name: t
steps:
  - {id: think, prompt: "hi", agent: {provider: claude, model: m1}}
  - {id: sh, shell: "echo x"}
  - {id: gate, needs: [think, sh], approve: {message: "ok?"}}
"""


async def test_request_carries_cost_sources(harness: Harness) -> None:
    harness.workflow("t", WF)
    seen: list[ApprovalRequest] = []

    async def prompt(request: ApprovalRequest) -> ApprovalAnswer:
        seen.append(request)
        return ApprovalAnswer(True, "")

    stub = StubProvider(
        script={"steps": {"think": {"text": "t", "usage": {"input": 1000, "output": 500}}}}
    )
    runner = harness.runner("t", providers={"claude": stub}, prompt=prompt)
    runner.price_table = PriceTable.from_config(
        {"m1": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}
    )
    result = await runner.run()
    assert result.status is RunStatus.SUCCEEDED
    (request,) = seen
    by_path = {n.path: n for n in request.needs}
    assert by_path["think"].cost_source == "table" and by_path["think"].cost_usd == pytest.approx(
        0.002
    )
    assert by_path["sh"].cost_source == "none" and by_path["sh"].cost_usd is None
    assert request.totals["cost_source"] == "table"
    assert request.totals["cost_usd"] == pytest.approx(0.002)
    assert request.totals["tokens"] == 1500
