# SPDX-License-Identifier: Apache-2.0
"""Where ``defaults.on_step_failure`` comes from for one sibling list.

The policy is lexically scoped, like ``defaults.timeout``: an ``include:``d workflow that states
one governs its own body, one that says nothing inherits the including run's, and ``--fail-fast``
tightens every scope from the outside. ``loop:``/``each:`` bodies share their parent's defaults
and therefore always inherit.

Nesting only ever TIGHTENS (``continue < drain < fail_fast``): a block may make its own body more
careful than the run that includes it, never less.
"""

from __future__ import annotations

import anyio
import pytest

from rayspec.engine.context import (
    ON_STEP_FAILURE_ORDER,
    RunOptions,
    StepOutcome,
    strictest_on_step_failure,
)
from rayspec.engine.scheduler import run_graph
from rayspec.schema import StepStatus

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


def wf(steps: str, **defaults: object) -> str:
    extra = "\n".join(f"  {k}: {v}" for k, v in defaults.items())
    block = f"defaults:\n{extra}\n" if defaults else ""
    return f"rayspec: 1\nname: t\n{block}steps:\n{steps}"


BLOCK = """
rayspec: 1
name: block
defaults:
  on_step_failure: {policy}
steps:
  - {{id: bad, shell: fail}}
  - {{id: indep, shell: ok}}
  - {{id: later, needs: [indep], shell: ok}}
"""

PLAIN_BLOCK = """
rayspec: 1
name: block
steps:
  - {id: bad, shell: fail}
  - {id: indep, shell: ok}
  - {id: later, needs: [indep], shell: ok}
"""


async def test_included_workflow_governs_its_own_body(harness: Harness) -> None:
    """An ``include:``d workflow that states ``on_step_failure`` decides for its own steps."""
    harness.workflow("block", BLOCK.format(policy="continue"))
    harness.workflow("t", wf("  - {id: run_block, include: block}\n"))
    g = make_graph_harness(harness, harness.load("t"))
    await run_graph(g.graph, g.scope, g.ctx)
    statuses = harness.statuses(g.run.run_id)
    assert statuses["run_block/bad"] == "failed"
    # the root never wrote the key, so it asked for nothing and sets no floor; the body
    # said ``continue``, so its own branches keep going
    assert statuses["run_block/later"] == "succeeded"


async def test_included_workflow_without_a_policy_inherits_the_run(harness: Harness) -> None:
    """Silence in the included workflow keeps the including run's policy — the 1.0.0 shape."""
    harness.workflow("block", PLAIN_BLOCK)
    harness.workflow("t", wf("  - {id: run_block, include: block}\n", on_step_failure="continue"))
    g = make_graph_harness(harness, harness.load("t"))
    await run_graph(g.graph, g.scope, g.ctx)
    assert harness.statuses(g.run.run_id)["run_block/later"] == "succeeded"


async def test_included_workflow_can_tighten_to_drain(harness: Harness) -> None:
    """``drain`` written in the body beats ``continue`` on the root: it is a statement, not a
    default."""
    harness.workflow("block", BLOCK.format(policy="drain"))
    harness.workflow("t", wf("  - {id: run_block, include: block}\n", on_step_failure="continue"))
    g = make_graph_harness(harness, harness.load("t"))
    await run_graph(g.graph, g.scope, g.ctx)
    statuses = harness.statuses(g.run.run_id)
    assert statuses["run_block/bad"] == "failed"
    assert statuses["run_block/later"] == "skipped"
    assert harness.record(g.run.run_id).steps["run_block/later"].skip_reason == "run_failed"


async def test_included_fail_fast_does_not_escape_the_body(harness: Harness) -> None:
    """A body that asks for ``fail_fast`` cancels its own siblings, not the including run's."""
    harness.workflow(
        "block",
        """
rayspec: 1
name: block
defaults:
  on_step_failure: fail_fast
steps:
  - {id: warmup, shell: "sleep:0.01"}
  - {id: bad, needs: [warmup], shell: fail}
  - {id: slow, shell: hang}
  - {id: slower, shell: hang}
""",
    )
    harness.workflow(
        "t",
        wf("""
  - {id: outer, shell: block}
  - {id: run_block, include: block}
  - {id: after, needs: [run_block], join: always, shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))

    async def release_outer_once_the_root_has_decided() -> None:
        """Let ``outer`` finish, but not before the root has settled the failed body.

        That settle is the moment a leaked ``fail_fast`` would cancel ``outer``, so it is the
        only moment this test can observe the leak — ``outer`` must still be running then. A
        ``sleep:`` step only guesses at that (the whole window measured about 35 ms wide, and
        the leak goes unseen the moment the guess is off), so ``outer`` is held open instead.
        ``after`` is decided only once ``run_block`` has settled: it starting IS the root
        having made its call.
        """
        while "after" not in g.leaf.started:
            await anyio.sleep(0.005)
        g.leaf.release.set()

    # bound inside the group below, and a task group is allowed to swallow what its body raised
    # — so this stays None if the run never returned, and the assertion after it says so
    outcomes: dict[str, StepOutcome] | None = None
    with anyio.fail_after(15):  # hang detector: every wait here is on observed state
        async with anyio.create_task_group() as tg:
            tg.start_soon(release_outer_once_the_root_has_decided)
            outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes is not None, "the graph never returned its outcomes"
    statuses = harness.statuses(g.run.run_id)
    assert statuses["run_block/slow"] == "interrupted"
    # the including graph never entered fail-fast: its own sibling ran to completion. Note
    # ``run_block`` carries no ``allow_failure:`` — a tolerated failure never arms fail-fast at
    # all, so this line could not tell a contained policy from a leaked one.
    assert outcomes["outer"].record.status is StepStatus.SUCCEEDED


async def test_cli_fail_fast_still_tightens_an_included_body(harness: Harness) -> None:
    """``--fail-fast`` is an operator override: it reaches into every scope."""
    harness.workflow("block", BLOCK.format(policy="continue"))
    harness.workflow("t", wf("  - {id: run_block, include: block, allow_failure: true}\n"))
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(fail_fast=True))
    with anyio.fail_after(5):
        await run_graph(g.graph, g.scope, g.ctx)
    assert harness.statuses(g.run.run_id)["run_block/later"] != "succeeded"


# --------------------------------------------------------------------------------------------------
# nesting only ever tightens
# --------------------------------------------------------------------------------------------------


def test_the_strictness_order_is_written_down() -> None:
    """``continue < drain < fail_fast``: the order every nested scope is clamped against."""
    assert ON_STEP_FAILURE_ORDER == ("continue", "drain", "fail_fast")
    assert strictest_on_step_failure("continue", "fail_fast") == "fail_fast"
    assert strictest_on_step_failure("drain", "continue") == "drain"
    assert strictest_on_step_failure("drain", "drain") == "drain"


async def test_an_included_workflow_cannot_relax_the_including_run(harness: Harness) -> None:
    """``continue`` in a block never loosens a run that asked for ``fail_fast``.

    ``on_step_failure`` is a blast-radius control: the root author asked for new work to stop the
    moment something fails. A block that states ``continue`` may only tighten, so the strictest of
    the two stands and the block's independent branch is not scheduled after ``bad`` failed.
    """
    harness.workflow("block", BLOCK.format(policy="continue"))
    harness.workflow("t", wf("  - {id: run_block, include: block}\n", on_step_failure="fail_fast"))
    g = make_graph_harness(harness, harness.load("t"))
    with anyio.fail_after(5):
        await run_graph(g.graph, g.scope, g.ctx)
    statuses = harness.statuses(g.run.run_id)
    assert statuses["run_block/bad"] == "failed"
    assert statuses["run_block/later"] == "skipped", "a block must not relax the run's fail_fast"


async def test_an_included_workflow_cannot_relax_a_run_that_drains(harness: Harness) -> None:
    """The same rule one notch down: ``continue`` in a block does not undo the run's ``drain``."""
    harness.workflow("block", BLOCK.format(policy="continue"))
    harness.workflow("t", wf("  - {id: run_block, include: block}\n", on_step_failure="drain"))
    g = make_graph_harness(harness, harness.load("t"))
    await run_graph(g.graph, g.scope, g.ctx)
    statuses = harness.statuses(g.run.run_id)
    assert statuses["run_block/later"] == "skipped"
    assert harness.record(g.run.run_id).steps["run_block/later"].skip_reason == "run_failed"


async def test_a_nested_include_cannot_relax_an_outer_block(harness: Harness) -> None:
    """Tightening accumulates: an inner block sees the strictest policy of every enclosing scope."""
    harness.workflow("inner", BLOCK.format(policy="continue"))
    harness.workflow(
        "outer",
        """
rayspec: 1
name: outer
defaults:
  on_step_failure: drain
steps:
  - {id: nested, include: inner}
""",
    )
    harness.workflow("t", wf("  - {id: run_block, include: outer}\n"))
    g = make_graph_harness(harness, harness.load("t"))
    await run_graph(g.graph, g.scope, g.ctx)
    assert harness.statuses(g.run.run_id)["run_block/nested/later"] == "skipped"
