# SPDX-License-Identifier: Apache-2.0
"""The failure policy a run started with is the policy it is resumed with.

``run.json`` already remembers ``--dry-run`` and the ``--stubs`` file so that a resume behaves
like the run it continues. ``--fail-fast`` did not survive: the second half of a run would let
siblings drain that the first half would have cancelled — the same run, two blast radii,
depending only on where it was interrupted.

A workflow's own ``defaults.on_step_failure`` needs no recording; it is part of the workflow, and
the workflow hash already refuses a resume of a changed one. The flag is the part that lives on
the command line, and (like everything about a failure policy) a later entry may only ever
TIGHTEN it.
"""

from __future__ import annotations

import anyio
import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.scheduler import run_graph
from rayspec.schema import StepStatus

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio

TINY = """rayspec: 1
name: tiny
steps:
  - {id: only, shell: ok}
"""

SIBLINGS = """rayspec: 1
name: t
steps:
  - {id: bad, shell: "sleep:0.01"}
  - {id: bad2, needs: [bad], shell: fail}
  - {id: slow, shell: hang}
  - {id: later, needs: [slow], shell: ok}
"""


async def test_a_run_records_the_flag_it_started_with(harness: Harness) -> None:
    harness.workflow("tiny", TINY)
    result = await harness.run("tiny", options=RunOptions(fail_fast=True))
    assert harness.record(result.run_id).fail_fast is True


async def test_a_run_without_the_flag_records_no_policy_of_its_own(harness: Harness) -> None:
    harness.workflow("tiny", TINY)
    result = await harness.run("tiny")
    assert harness.record(result.run_id).fail_fast is False


async def test_the_recorded_flag_cancels_siblings_without_being_passed_again(
    harness: Harness,
) -> None:
    """What a resume entry point that passed no flag must still do."""
    harness.workflow("t", SIBLINGS)
    g = make_graph_harness(harness, harness.load("t"))
    g.run.fail_fast = True  # what `--fail-fast` at launch left in run.json
    with anyio.fail_after(5):
        outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["bad2"].record.status is StepStatus.FAILED
    assert outcomes["slow"].record.status is StepStatus.INTERRUPTED
    assert outcomes["slow"].record.skip_reason == "failed"


CONTINUE = """rayspec: 1
name: t
defaults:
  on_step_failure: continue
steps:
  - {id: bad, shell: fail}
  - {id: busy, shell: "sleep:0.05"}
  - {id: indep, needs: [busy], shell: ok}
"""

#: The same workflow with the sibling held open instead of sitting on a timer. Cancellation only
#: reaches a sibling that is STILL RUNNING when the failure is settled (``scheduler.run_graph``),
#: so a 50 ms sleep makes the assertion a race — one a loaded runner loses, and ``busy`` then
#: reports SUCCEEDED. ``block`` runs until the harness releases it, which it never does here.
CONTINUE_BLOCKING = CONTINUE.replace('"sleep:0.05"', "block")
assert "block" in CONTINUE_BLOCKING, "CONTINUE no longer holds the body this test rewrites"


async def test_the_recorded_flag_also_beats_on_step_failure_continue(harness: Harness) -> None:
    """A recorded ``--fail-fast`` tightens a workflow that asked to keep going, as the flag does.

    Without it ``busy`` finishes and ``indep`` launches after the failure; with it the running
    sibling is cancelled and nothing new starts.
    """
    harness.workflow("t", CONTINUE_BLOCKING)
    g = make_graph_harness(harness, harness.load("t"))
    g.run.fail_fast = True
    with anyio.fail_after(5):
        outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["bad"].record.status is StepStatus.FAILED
    assert outcomes["busy"].record.status is StepStatus.INTERRUPTED
    assert outcomes["indep"].record.status is StepStatus.SKIPPED


async def test_a_workflow_that_keeps_going_still_does_without_the_flag(harness: Harness) -> None:
    """The other half of the pair: nothing recorded, and ``continue`` means what it says."""
    harness.workflow("t", CONTINUE)
    g = make_graph_harness(harness, harness.load("t"))
    with anyio.fail_after(5):
        outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["bad"].record.status is StepStatus.FAILED
    assert outcomes["indep"].record.status is StepStatus.SUCCEEDED


async def test_a_resume_may_tighten_the_recorded_policy(harness: Harness) -> None:
    """``rayspec resume --fail-fast`` on a run that started without it, and it stays recorded."""
    harness.workflow("tiny", TINY)
    first = await harness.run("tiny")
    assert harness.record(first.run_id).fail_fast is False
    await harness.run("tiny", options=RunOptions(resume=True, fail_fast=True), resume=first.run_id)
    assert harness.record(first.run_id).fail_fast is True


async def test_a_resume_without_the_flag_never_loosens_the_record(harness: Harness) -> None:
    harness.workflow("tiny", TINY)
    first = await harness.run("tiny", options=RunOptions(fail_fast=True))
    await harness.run("tiny", options=RunOptions(resume=True), resume=first.run_id)
    assert harness.record(first.run_id).fail_fast is True
