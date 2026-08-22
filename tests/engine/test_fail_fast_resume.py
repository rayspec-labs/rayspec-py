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

RUN_ID = "20260822-000000-ffr1"


async def _approve(_request: ApprovalRequest) -> ApprovalAnswer:
    return ApprovalAnswer(True, "")


async def _run(harness: Harness, options: RunOptions, **kw: Any) -> RunResult:
    runner = Runner(
        harness.load("t"),
        inputs={},
        store=harness.store,
        sinks=harness.sink,
        project_root=harness.root,
        project_slug="local/test",
        engine=harness.engine,
        options=options,
        executors={"shell": FakeLeaf(), "python": FakeLeaf()},
        **kw,
    )
    with anyio.fail_after(15):
        return await runner.run()


async def launch(harness: Harness, *, fail_fast: bool = False) -> RunResult:
    """The first half: it pauses at the gate."""
    return await _run(harness, RunOptions(interactive=False, fail_fast=fail_fast), run_id=RUN_ID)


async def resume(harness: Harness, *, fail_fast: bool = False) -> RunResult:
    """The second half: the gate is approved and the steps behind it run."""
    return await _run(
        harness,
        RunOptions(fail_fast=fail_fast),
        resume_run_id=RUN_ID,
        approval_prompt=_approve,
    )


@pytest.fixture
def gated(harness: Harness) -> Harness:
    harness.workflow("t", GATED)
    return harness


async def test_fail_fast_is_recorded_and_survives_a_resume(gated: Harness) -> None:
    """Launched with ``--fail-fast``; the resumed half still cancels the running sibling."""
    first = await launch(gated, fail_fast=True)
    assert first.status is RunStatus.PAUSED
    assert gated.record(RUN_ID).fail_fast is True, "the effective policy must be in run.json"

    second = await resume(gated)
    assert second.status is RunStatus.FAILED
    statuses = gated.statuses(RUN_ID)
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


async def test_a_resume_may_tighten_a_run_that_was_launched_without_it(gated: Harness) -> None:
    """``rayspec resume --fail-fast``: the override may be supplied late, and it sticks."""
    await launch(gated)
    second = await resume(gated, fail_fast=True)
    assert second.status is RunStatus.FAILED
    assert gated.statuses(RUN_ID)["slow"] == StepStatus.INTERRUPTED.value
    assert gated.record(RUN_ID).fail_fast is True, "the tightened policy is recorded in turn"


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
