# SPDX-License-Identifier: Apache-2.0
"""``--fail-fast`` is part of the run, so it survives every resume entry.

The failure policy is a blast-radius control: it decides whether a failure cancels the siblings
that are still running. ``--fail-fast`` is the operator's override of it, and it lives on the
command line — so a run launched with it and continued by ``rayspec resume``/``approve``/
``reject`` used to run its second half under a *different*, looser policy than its first, with
nothing saying so. The workflow's own ``defaults.on_step_failure`` never had that problem: it is
in the file both halves read.

``run.json`` therefore records the effective override, every resume entry restores it, and
``rayspec resume --fail-fast`` can still tighten a run that was launched without it (the flag
may only ever tighten — that is the rule ``fail_fast_for`` already applies within one run).
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import anyio
import pytest

from rayspec.engine.approval import ApprovalAnswer, ApprovalRequest
from rayspec.engine.context import RunOptions
from rayspec.engine.runner import Runner, RunResult
from rayspec.schema import RunStatus, StepStatus

from .conftest import FakeLeaf, Harness

pytestmark = pytest.mark.anyio

GATED = """
rayspec: 1
name: t
steps:
  - {id: gate, approve: "go?"}
  - {id: boom, needs: [gate], shell: fail}
  - {id: slow, needs: [gate], shell: "sleep:0.5"}
"""

#: The same run with both halves of the moment under the test's control, for the two tests that
#: assert a *cancellation*. ``run_graph`` cancels siblings only ``if state["draining"] and
#: fail_fast and running``, so what those tests are about is ``slow`` still RUNNING at the
#: instant ``boom``'s failure settles — a state, and one a sleep can only guess at: half a
#: second is bet against the engine's own per-step bookkeeping, which is two threaded fsynced
#: writes of ``run.json`` and grows with the load on the machine.
#:
#: ``block:slow`` holds the sibling open for as long as the run lasts, so "still running" stops
#: depending on the clock. ``boom`` moves behind ``hold`` for the other half: the two siblings
#: of one gate become ready in the same instant, which of them the scheduler reaches first is
#: the machine's business, and the failure must not be allowed to settle before ``slow`` has
#: said it is in its body.
HELD = """
rayspec: 1
name: t
steps:
  - {id: gate, approve: "go?"}
  - {id: hold, needs: [gate], shell: "block:hold"}
  - {id: boom, needs: [hold], shell: fail}
  - {id: slow, needs: [gate], shell: "block:slow"}
"""

RUN_ID = "20260822-000000-ffr1"


async def _approve(_request: ApprovalRequest) -> ApprovalAnswer:
    return ApprovalAnswer(True, "")


async def _run(harness: Harness, options: RunOptions, leaf: FakeLeaf, **kw: Any) -> RunResult:
    runner = Runner(
        harness.load("t"),
        inputs={},
        store=harness.store,
        sinks=harness.sink,
        project_root=harness.root,
        project_slug="local/test",
        engine=harness.engine,
        options=options,
        executors={"shell": leaf, "python": leaf},
        **kw,
    )
    with anyio.fail_after(15):
        return await runner.run()


async def launch(harness: Harness, *, fail_fast: bool = False) -> RunResult:
    """The first half: it pauses at the gate (no leaf step of its own runs)."""
    return await _run(
        harness, RunOptions(interactive=False, fail_fast=fail_fast), FakeLeaf(), run_id=RUN_ID
    )


async def resume(
    harness: Harness, *, fail_fast: bool = False, leaf: FakeLeaf | None = None
) -> RunResult:
    """The second half: the gate is approved and the steps behind it run."""
    return await _run(
        harness,
        RunOptions(fail_fast=fail_fast),
        leaf or FakeLeaf(),
        resume_run_id=RUN_ID,
        approval_prompt=_approve,
    )


async def resume_with_slow_in_flight(harness: Harness, *, fail_fast: bool = False) -> RunResult:
    """Resume a :data:`HELD` run, holding the failure back until ``slow`` is provably running.

    The overlap the cancellation tests are about is established here rather than hoped for:
    ``boom`` sits behind ``hold``, and ``hold`` is released only once ``slow`` has signalled
    that it entered its body. What comes back is the whole run's result — fail-fast cancels the
    blocked ``slow``, which is what lets the run end at all.

    Neither wait can deadlock, and neither can hang the suite. ``hold`` blocking yields to the
    scheduler, which has three of its four slots free and launches ``slow``, which signals its
    arrival before waiting on anything itself; ``wait_started`` names the step that never
    arrived, and a run that keeps draining a sibling nothing will ever release runs into its own
    ``fail_after`` — caught here so that the regression is reported by name rather than as a
    stray clock.
    """
    leaf = FakeLeaf()
    results: list[RunResult] = []

    async def _resume() -> None:
        with contextlib.suppress(TimeoutError):  # reported by the assertion below
            results.append(await resume(harness, fail_fast=fail_fast, leaf=leaf))

    async with anyio.create_task_group() as tg:
        tg.start_soon(_resume)
        await leaf.wait_started("slow")  # the fact: the sibling is inside its body
        leaf.releases["hold"].set()  # only now may `boom` reach its failure
    assert results, (
        "the run never wound down: `boom` failed with `slow` held open and nothing cancelled "
        "it, so the graph is still draining a step that will never finish — the policy in "
        "force was not fail-fast"
    )
    return results[0]


@pytest.fixture
def gated(harness: Harness) -> Harness:
    harness.workflow("t", GATED)
    return harness


@pytest.fixture
def held(harness: Harness) -> Harness:
    """:data:`GATED` with the failure and the sibling both under the test's control."""
    harness.workflow("t", HELD)
    return harness


async def test_fail_fast_is_recorded_and_survives_a_resume(held: Harness) -> None:
    """Launched with ``--fail-fast``; the resumed half still cancels the running sibling."""
    first = await launch(held, fail_fast=True)
    assert first.status is RunStatus.PAUSED
    assert held.record(RUN_ID).fail_fast is True, "the effective policy must be in run.json"

    second = await resume_with_slow_in_flight(held)
    assert second.status is RunStatus.FAILED
    statuses = held.statuses(RUN_ID)
    assert statuses["boom"] == "failed"
    assert statuses["slow"] == StepStatus.INTERRUPTED.value, (
        "the second half ran under the policy the run was launched with"
    )


async def test_without_the_flag_the_resumed_half_still_drains(gated: Harness) -> None:
    """The control: a run launched without it keeps draining, and records that."""
    first = await launch(gated)
    assert first.status is RunStatus.PAUSED
    assert gated.record(RUN_ID).fail_fast is False

    second = await resume(gated)
    assert second.status is RunStatus.FAILED
    assert gated.statuses(RUN_ID)["slow"] == "succeeded", "drain lets the sibling finish"


async def test_a_resume_may_tighten_a_run_that_was_launched_without_it(held: Harness) -> None:
    """``rayspec resume --fail-fast``: the override may be supplied late, and it sticks."""
    await launch(held)
    second = await resume_with_slow_in_flight(held, fail_fast=True)
    assert second.status is RunStatus.FAILED
    assert held.statuses(RUN_ID)["slow"] == StepStatus.INTERRUPTED.value
    assert held.record(RUN_ID).fail_fast is True, "the tightened policy is recorded in turn"


async def test_a_resume_without_the_flag_never_loosens_a_recorded_one(gated: Harness) -> None:
    """It may only tighten: omitting the flag on a resume does not turn fail-fast off."""
    await launch(gated, fail_fast=True)
    await resume(gated)
    assert gated.record(RUN_ID).fail_fast is True


async def test_an_older_record_without_the_field_resumes_as_drain(gated: Harness) -> None:
    """A ``run.json`` written before the field existed reads ``false`` — the old behaviour."""
    await launch(gated)
    path = gated.store.run_dir(RUN_ID) / "run.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["fail_fast"]
    path.write_text(json.dumps(data), encoding="utf-8")

    second = await resume(gated)
    assert second.status is RunStatus.FAILED
    assert gated.statuses(RUN_ID)["slow"] == "succeeded"
