# SPDX-License-Identifier: Apache-2.0
"""A cleanup step that PAUSES while a sibling list is being wound down.

``join: always`` makes the finally idiom reachable from a teardown, and a cleanup step is exactly
where a human gate belongs ("shall I roll back?"). The wind-down therefore has to survive a
non-terminal outcome: it must stop deciding, leave the rest of the list undecided for the resumed
run, and bubble the pause rather than the signal that started the teardown — otherwise the run
ends on an engine error and no resume can ever reach the cleanup.
"""

from __future__ import annotations

import anyio
import pytest

from rayspec.engine.approval import ApprovalAnswer, ApprovalRequest
from rayspec.engine.context import RunOptions
from rayspec.engine.errors import RunPaused
from rayspec.engine.scheduler import run_graph
from rayspec.schema import RunStatus, StepStatus

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


def wf(steps: str) -> str:
    return f"rayspec: 1\nname: t\nsteps:\n{steps}"


async def test_a_gate_that_pauses_in_a_stop_wind_down_pauses_the_run(harness: Harness) -> None:
    """The pause beats the ``stop:`` that tore the list down, and nothing after it is decided."""
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: ok}
  - {id: halt, needs: [a], stop: {status: succeeded, reason: "operator asked to stop"}}
  - {id: slow, shell: hang}
  - {id: gate, needs: [halt], join: always, approve: "roll back?"}
  - {id: rollback, needs: [gate], join: always, shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(interactive=False))
    with anyio.fail_after(5), pytest.raises(RunPaused) as info:
        await run_graph(g.graph, g.scope, g.ctx)
    assert info.value.token == "gate#1"
    run = harness.record(g.run.run_id)
    assert run.steps["gate"].status is StepStatus.PAUSED
    assert "rollback" not in run.steps, "a pause never records the steps it left pending"


async def test_a_composite_whose_body_pauses_ends_the_wind_down(harness: Harness) -> None:
    """A ``join: always`` composite is recorded ``paused`` too — that record is not terminal."""
    harness.workflow(
        "block",
        "rayspec: 1\nname: block\nsteps:\n  - {id: gate, approve: 'roll back?'}\n",
    )
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: ok}
  - {id: halt, needs: [a], stop: {status: succeeded, reason: bye}}
  - {id: slow, shell: hang}
  - {id: cleanup, needs: [halt], join: always, include: block}
  - {id: rollback, needs: [cleanup], join: always, shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(interactive=False))
    with anyio.fail_after(5), pytest.raises(RunPaused):
        await run_graph(g.graph, g.scope, g.ctx)
    run = harness.record(g.run.run_id)
    assert run.steps["cleanup"].status is StepStatus.PAUSED
    assert "rollback" not in run.steps


# --------------------------------------------------------------------------------------------------
# end to end: the run pauses, and the resumed run reaches the cleanup
# --------------------------------------------------------------------------------------------------


REAL_STOP_THEN_GATE = """
rayspec: 1
name: t
steps:
  - {id: a, shell: "true"}
  - {id: halt, needs: [a], stop: {status: succeeded, reason: "operator asked to stop"}}
  - {id: gate, needs: [halt], join: always, approve: "roll back?"}
  - {id: rollback, needs: [gate], join: always, shell: "true"}
"""


async def test_the_run_pauses_and_the_resumed_run_rolls_back(harness: Harness) -> None:
    """The operator's declared outcome survives, and the gate stays answerable.

    A run recorded ``failed`` could not be approved and resumed the same crash every time, so the
    ``join: always`` cleanup was unreachable on every attempt.
    """
    harness.workflow("t", REAL_STOP_THEN_GATE)
    first = await harness.run(
        "t", run_id="20260820-000000-wdp", options=RunOptions(interactive=False)
    )
    assert first.status is RunStatus.PAUSED and first.exit_code == 3
    assert first.pause is not None and first.pause.step == "gate"
    assert "rollback" not in harness.record("20260820-000000-wdp").steps

    async def approve(request: ApprovalRequest) -> ApprovalAnswer:
        return ApprovalAnswer(True, "")

    second = await harness.run("t", resume="20260820-000000-wdp", prompt=approve)
    statuses = harness.statuses("20260820-000000-wdp")
    assert statuses["gate"] == "succeeded"
    assert statuses["rollback"] == "succeeded"
    assert second.status is RunStatus.SUCCEEDED and second.reason == "operator asked to stop"
