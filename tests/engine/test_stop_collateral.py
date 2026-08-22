# SPDX-License-Identifier: Apache-2.0
"""A ``stop:`` that has to cancel a RUNNING sibling keeps its declared status.

Cancelling the siblings is how a ``stop:`` ends a sibling list: the running ones are recorded
``interrupted`` with the skip reason ``stopped``. Those records are the stop's own teardown, not
independent failures — but they are ``FAILED_LIKE``, so the run-level rule "a failure somewhere
else means the stop is laundering it" used to count them and report a declared, deliberate stop
as ``failed`` with the collateral as its reason. Whether that happened depended only on whether a
sibling happened to still be running, which is timing.

The rule that must hold: what the stop tore down is explained by the stop; only a step that
failed on its OWN — or was interrupted by something else — outranks the signal.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.runner import Runner, RunResult
from rayspec.schema import RunStatus, StepStatus

from .conftest import FakeLeaf, Harness

pytestmark = pytest.mark.anyio


def wf(steps: str, **top: str) -> str:
    extra = "".join(f"{k}:\n{v}\n" for k, v in top.items())
    return f"rayspec: 1\nname: t\n{extra}steps:\n{steps}"


async def run_with_fake_leaf(harness: Harness, name: str, **kw: Any) -> RunResult:
    """A full run (runner + ``_finalize``) whose ``shell:`` steps are the FakeLeaf.

    ``hang`` gives a sibling that is reliably still in flight when the ``stop:`` fires, without
    a real subprocess and without a sleep in the suite.
    """
    runner = Runner(
        harness.load(name),
        inputs=kw.pop("inputs", None) or {},
        store=harness.store,
        sinks=harness.sink,
        project_root=harness.root,
        project_slug="local/test",
        engine=harness.engine,
        options=kw.pop("options", None) or RunOptions(interactive=False),
        executors={"shell": FakeLeaf(), "python": FakeLeaf()},
        handle_signals=True,
        **kw,
    )
    with anyio.fail_after(10):
        return await runner.run()


WF_STOP_BODY = '  - {id: halt, stop: {status: cancelled, reason: "stopping on purpose"}}\n'

STOP_WITH_SIBLING = wf(
    """
  - {id: assess, shell: ok}
  - {id: slow_a, shell: hang}
  - {id: halt, needs: [assess], stop: {status: cancelled, reason: "stopping on purpose"}}
""",
    outputs='  verdict: "{{ steps.assess.output }}"',
)


async def test_a_stop_that_cancels_a_running_sibling_keeps_its_status(harness: Harness) -> None:
    """exit 4 / ``cancelled`` / the stop's own reason — the same answer as with nothing in flight."""
    harness.workflow("t", STOP_WITH_SIBLING)
    result = await run_with_fake_leaf(harness, "t")
    assert result.status is RunStatus.CANCELLED, "the stop's declared status must survive"
    assert result.exit_code == 4
    assert result.reason == "stopping on purpose"
    run = harness.record(result.run_id)
    assert run.status is RunStatus.CANCELLED and run.reason == "stopping on purpose"
    # the collateral is still recorded honestly — it is only not counted as a failure
    assert run.steps["slow_a"].status is StepStatus.INTERRUPTED
    assert run.steps["slow_a"].skip_reason == "stopped"


async def test_the_same_workflow_with_nothing_in_flight_is_unchanged(harness: Harness) -> None:
    """The control: the answer above is the one this shape already gave."""
    harness.workflow(
        "t",
        wf(
            """
  - {id: assess, shell: ok}
  - {id: halt, needs: [assess], stop: {status: cancelled, reason: "stopping on purpose"}}
""",
            outputs='  verdict: "{{ steps.assess.output }}"',
        ),
    )
    result = await run_with_fake_leaf(harness, "t")
    assert result.status is RunStatus.CANCELLED and result.exit_code == 4
    assert result.reason == "stopping on purpose"


async def test_a_stop_that_succeeds_still_publishes_outputs(harness: Harness) -> None:
    """``outputs:`` are published for a ``stop: {status: succeeded}`` with a sibling in flight.

    A run whose only "failure" was the teardown it ordered itself is a succeeded run, and a
    succeeded run publishes its outputs — otherwise the caller reads ``null`` for a workflow
    that answered, purely because of scheduling.
    """
    harness.workflow(
        "t",
        wf(
            """
  - {id: assess, shell: "ok:green"}
  - {id: slow_a, shell: hang}
  - {id: halt, needs: [assess], stop: {status: succeeded, reason: "nothing to do"}}
""",
            outputs='  verdict: "{{ steps.assess.output }}"',
        ),
    )
    result = await run_with_fake_leaf(harness, "t")
    assert result.status is RunStatus.SUCCEEDED and result.exit_code == 0
    assert result.reason == "nothing to do"
    assert result.outputs == {"verdict": "green"}
    assert harness.record(result.run_id).outputs == {"verdict": "green"}


async def test_a_real_failure_still_outranks_the_stop(harness: Harness) -> None:
    """The laundering guard is untouched: a step that failed on its own still fails the run."""
    harness.workflow(
        "t",
        wf(
            """
  - {id: boom, shell: fail}
  - {id: wait, shell: ok}
  - {id: slow_a, shell: hang}
  - {id: finish, needs: [wait], stop: {status: succeeded, reason: "report published"}}
""",
            defaults="  on_step_failure: continue",
        ),
    )
    result = await run_with_fake_leaf(harness, "t")
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    assert result.reason and "boom" in result.reason
    assert result.outputs is None


async def test_an_interrupted_sibling_of_a_failure_still_fails_the_run(harness: Harness) -> None:
    """``--fail-fast`` collateral is NOT a stop's teardown: it stays a failure of the run.

    The exemption is keyed on the skip reason the scheduler recorded (``stopped``), so a sibling
    cancelled because something FAILED (skip reason ``failed``) keeps counting.
    """
    harness.workflow(
        "t",
        wf(
            """
  - {id: boom, shell: fail}
  - {id: slow_a, shell: hang}
""",
            defaults="  on_step_failure: fail_fast",
        ),
    )
    result = await run_with_fake_leaf(harness, "t")
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    assert harness.record(result.run_id).steps["slow_a"].status is StepStatus.INTERRUPTED


async def test_a_composite_carrying_the_stop_is_not_counted_either(harness: Harness) -> None:
    """The same shape one level in: an ``each:``/``include:`` whose BODY stopped.

    The composite step is the carrier of the signal — the scheduler records it ``interrupted``
    with the reason ``stopped`` — while ``ctx.stopped.step_path`` names the inner ``stop:``. So
    the top-level exemption for the stop's own step never covered it, and any workflow that puts
    its ``stop:`` inside a block reported ``failed`` no matter what it declared.
    """
    harness.workflow("halted", "rayspec: 1\nname: halted\nsteps:\n" + WF_STOP_BODY)
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: ok}
  - {id: block, needs: [a], include: halted}
"""),
    )
    result = await run_with_fake_leaf(harness, "t")
    assert result.status is RunStatus.CANCELLED and result.exit_code == 4
    assert result.reason == "stopping on purpose"
    assert harness.record(result.run_id).steps["block"].status is StepStatus.INTERRUPTED


async def test_the_pause_banner_does_not_call_the_teardown_victims_failed(
    harness: Harness,
) -> None:
    """A gate that pauses during a ``stop:`` wind-down: the reason must not claim a failure.

    The skip reason recorded one line away says ``stopped``; the pause banner said "1 step(s)
    already failed", which is the same miscount one level up — and it is the line an operator
    reads before deciding whether to approve the rollback.
    """
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: ok}
  - {id: slow_a, shell: hang}
  - {id: halt, needs: [a], stop: {status: cancelled, reason: "stopping on purpose"}}
  - {id: gate, needs: [halt], join: always, approve: "roll back?"}
"""),
    )
    result = await run_with_fake_leaf(harness, "t")
    assert result.status is RunStatus.PAUSED and result.exit_code == 3
    assert result.reason == "awaiting approval at gate"
    assert "already failed" not in (result.reason or "")
