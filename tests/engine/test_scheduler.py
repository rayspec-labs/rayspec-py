"""engine.scheduler with a fake leaf executor: ordering, limits, joins, when, drain/fail-fast,
stop, cancellation, always-during-drain."""

from __future__ import annotations

import anyio
import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.errors import RunPaused, RunStopped
from rayspec.engine.scheduler import run_graph
from rayspec.events.model import EventType
from rayspec.schema import StepStatus

from .conftest import Harness, make_graph_harness, wait_for_slot_queue

pytestmark = pytest.mark.anyio


def wf(steps: str, **defaults: object) -> str:
    extra = "\n".join(f"  {k}: {v}" for k, v in defaults.items())
    block = f"defaults:\n{extra}\n" if defaults else ""
    return f"rayspec: 1\nname: t\n{block}steps:\n{steps}"


async def test_linear_order(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: ok}
  - {id: b, needs: [a], shell: ok}
  - {id: c, needs: [b], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert g.leaf.started == ["a", "b", "c"]
    assert all(o.record.status is StepStatus.SUCCEEDED for o in outcomes.values())
    assert harness.statuses(g.run.run_id) == {"a": "succeeded", "b": "succeeded", "c": "succeeded"}
    # write-ahead: every succeeded step has an output file
    for rec in harness.record(g.run.run_id).steps.values():
        assert rec.output_ref and (harness.store.run_dir(g.run.run_id) / rec.output_ref).is_file()
    kinds = [e.type for e in harness.events()]
    assert kinds.count(EventType.STEP_STARTED) == 3
    assert kinds.count(EventType.STEP_FINISHED) == 3


async def test_diamond_runs_middle_in_parallel(harness: Harness) -> None:
    """``b`` and ``c`` really are in flight together, not merely quick enough to look like it."""
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: ok}
  - {id: b, needs: [a], shell: "block:b"}
  - {id: c, needs: [a], shell: "block:c"}
  - {id: d, needs: [b, c], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_graph, g.graph, g.scope, g.ctx)
        # The wait IS the assertion. Both middles are held inside their bodies, so neither can
        # finish and make room for the other: an engine that ran the diamond one branch at a
        # time never gets the second one here, and the wait fails naming the step that is missing.
        await g.leaf.wait_started("b", "c")
        assert g.leaf.peak == 2
        g.leaf.releases["b"].set()
        g.leaf.releases["c"].set()
    # ``a`` first and ``d`` last are the DAG's doing and hold whatever the machine is busy with;
    # which of ``b``/``c`` the scheduler reaches first is nobody's business, hence the set.
    assert g.leaf.started[0] == "a" and g.leaf.started[-1] == "d"
    assert set(g.leaf.started[1:3]) == {"b", "c"}
    assert g.leaf.peak == 2


async def test_max_parallel_limit_respected(harness: Harness) -> None:
    """Six ready steps under ``max_parallel: 2``: two run, the other four wait for a slot."""
    steps = "\n".join(f'  - {{id: s{i}, shell: "block:hold"}}' for i in range(6))
    harness.workflow("t", wf(steps, max_parallel=2))
    g = make_graph_harness(harness, harness.load("t"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_graph, g.graph, g.scope, g.ctx)
        # Not "all six have started": that is true with no cap at all — only later — so a test
        # that waits for arrivals and then reads ``peak`` passes with the limiter widened. Four
        # leaves QUEUED on the limiter is the fact an upper bound needs: everything the engine
        # is going to launch is launched, and the two holding slots are all it will run at once.
        await wait_for_slot_queue(g.ctx, 4)
        assert g.leaf.peak == 2
        g.leaf.releases["hold"].set()
    assert g.leaf.peak == 2  # …and it stayed the ceiling while the queue drained
    assert len(g.leaf.finished) == 6


@pytest.mark.parametrize(
    ("join", "expected"),
    [("all", "skipped"), ("any", "succeeded"), ("always", "succeeded")],
)
async def test_join_policies_with_one_skipped_need(
    harness: Harness, join: str, expected: str
) -> None:
    harness.workflow(
        "t",
        wf(f"""
  - {{id: a, shell: ok}}
  - {{id: b, when: "false", shell: ok}}
  - {{id: c, needs: [a, b], join: {join}, shell: ok}}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["b"].record.skip_reason == "when_false"
    assert outcomes["c"].record.status.value == expected
    if expected == "skipped":
        assert outcomes["c"].record.skip_reason == "upstream_skipped"


async def test_when_skip_cascades_synchronously(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: a, when: "1 == 2", shell: ok}
  - {id: b, needs: [a], shell: ok}
  - {id: c, needs: [b], shell: ok}
  - {id: d, needs: [c], join: always, shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert [outcomes[s].record.status.value for s in "abc"] == ["skipped"] * 3
    assert outcomes["b"].record.skip_reason == "upstream_skipped"
    assert outcomes["d"].record.status is StepStatus.SUCCEEDED
    assert g.leaf.started == ["d"]


@pytest.mark.parametrize(
    ("expr", "needle"),
    [("'yes'", "true/false"), ("steps.nope.output == 'x'", "nope")],
)
async def test_when_non_bool_or_error_fails_the_step(
    harness: Harness, expr: str, needle: str
) -> None:
    harness.workflow("t", wf(f"""  - {{id: a, when: "{expr}", shell: ok}}\n"""))
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["a"].record.status is StepStatus.FAILED
    assert outcomes["a"].record.error is not None
    assert needle in outcomes["a"].record.error.message
    assert g.leaf.started == []


async def test_drain_default_lets_running_finish_and_skips_new(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: bad, shell: fail}
  - {id: slow, shell: "sleep:0.05"}
  - {id: after, needs: [bad], shell: ok}
  - {id: indep, needs: [slow], shell: ok}
  - {id: cleanup, needs: [bad], join: always, shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["bad"].record.status is StepStatus.FAILED
    assert outcomes["slow"].record.status is StepStatus.SUCCEEDED  # running sibling finished
    assert outcomes["after"].record.skip_reason == "upstream_failed"
    assert outcomes["indep"].record.skip_reason == "run_failed"  # nothing new starts
    assert outcomes["cleanup"].record.status is StepStatus.SUCCEEDED  # always runs during drain
    assert "cleanup" in g.leaf.started


async def test_fail_fast_cancels_running_siblings(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: bad, shell: "sleep:0.01"}
  - {id: bad2, needs: [bad], shell: fail}
  - {id: slow, shell: hang}
  - {id: later, needs: [slow], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(fail_fast=True))
    with anyio.fail_after(5):
        outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["bad2"].record.status is StepStatus.FAILED
    assert outcomes["slow"].record.status is StepStatus.INTERRUPTED
    assert outcomes["slow"].record.skip_reason == "failed"
    assert outcomes["later"].record.status is StepStatus.SKIPPED
    assert harness.statuses(g.run.run_id)["slow"] == "interrupted"


async def test_on_step_failure_continue_keeps_independent_branches_running(
    harness: Harness,
) -> None:
    """``continue``: a failure stops only its own dependents, never the rest of the DAG."""
    harness.workflow(
        "t",
        wf(
            """
  - {id: bad, shell: fail}
  - {id: after, needs: [bad], shell: ok}
  - {id: indep, shell: ok}
  - {id: later, needs: [indep], shell: ok}
""",
            on_step_failure="continue",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["bad"].record.status is StepStatus.FAILED
    # the failed step's own dependents still skip — `continue` is not `allow_failure`
    assert outcomes["after"].record.skip_reason == "upstream_failed"
    # …but an independent branch runs to completion, including steps queued after the failure
    assert outcomes["indep"].record.status is StepStatus.SUCCEEDED
    assert outcomes["later"].record.status is StepStatus.SUCCEEDED
    assert "later" in g.leaf.started


async def test_on_step_failure_continue_still_pauses_on_a_control_signal(
    harness: Harness,
) -> None:
    """``continue`` relaxes *failure* draining only; a pause still stops new work."""
    harness.workflow(
        "t",
        wf(
            """
  - {id: gate, approve: "ok?"}
  - {id: indep, needs: [gate], shell: ok}
""",
            on_step_failure="continue",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"))
    with pytest.raises(RunPaused):
        await run_graph(g.graph, g.scope, g.ctx)
    assert "indep" not in g.leaf.started


async def test_cli_fail_fast_overrides_on_step_failure_continue(harness: Harness) -> None:
    """``--fail-fast`` can only tighten: it beats ``continue``."""
    harness.workflow(
        "t",
        wf(
            """
  - {id: bad, shell: "sleep:0.01"}
  - {id: bad2, needs: [bad], shell: fail}
  - {id: slow, shell: hang}
""",
            on_step_failure="continue",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(fail_fast=True))
    with anyio.fail_after(5):
        outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["slow"].record.status is StepStatus.INTERRUPTED


async def test_defaults_on_step_failure_fail_fast_cancels_running_siblings(
    harness: Harness,
) -> None:
    """``defaults.on_step_failure: fail_fast`` does what ``--fail-fast`` does."""
    harness.workflow(
        "t",
        wf(
            """
  - {id: bad, shell: "sleep:0.01"}
  - {id: bad2, needs: [bad], shell: fail}
  - {id: slow, shell: hang}
  - {id: later, needs: [slow], shell: ok}
""",
            on_step_failure="fail_fast",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"))  # no RunOptions(fail_fast=True)
    with anyio.fail_after(5):
        outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["bad2"].record.status is StepStatus.FAILED
    assert outcomes["slow"].record.status is StepStatus.INTERRUPTED
    assert outcomes["slow"].record.skip_reason == "failed"
    assert outcomes["later"].record.status is StepStatus.SKIPPED


async def test_defaults_on_step_failure_drain_is_the_default(harness: Harness) -> None:
    """The explicit ``drain`` spelling keeps 1.0.0 behaviour: running siblings finish."""
    harness.workflow(
        "t",
        wf(
            """
  - {id: bad, shell: fail}
  - {id: slow, shell: "sleep:0.01"}
  - {id: indep, needs: [slow], shell: ok}
""",
            on_step_failure="drain",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["bad"].record.status is StepStatus.FAILED
    assert outcomes["slow"].record.status is StepStatus.SUCCEEDED
    assert outcomes["indep"].record.skip_reason == "run_failed"


async def test_cli_fail_fast_overrides_defaults_drain(harness: Harness) -> None:
    """``--fail-fast`` wins over ``defaults.on_step_failure: drain``."""
    harness.workflow(
        "t",
        wf(
            """
  - {id: bad, shell: "sleep:0.01"}
  - {id: bad2, needs: [bad], shell: fail}
  - {id: slow, shell: hang}
""",
            on_step_failure="drain",
        ),
    )
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(fail_fast=True))
    with anyio.fail_after(5):
        outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["slow"].record.status is StepStatus.INTERRUPTED


async def test_allow_failure_is_tolerated(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: bad, shell: fail, allow_failure: true}
  - {id: after, needs: [bad], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    rec = outcomes["bad"].record
    assert rec.status is StepStatus.FAILED and rec.tolerated and rec.ok is False
    assert outcomes["after"].record.status is StepStatus.SUCCEEDED
    assert g.scope.views["bad"].resolve("ok") is False


async def test_stop_interrupts_siblings_and_bubbles(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: ok}
  - {id: halt, needs: [a], stop: {status: cancelled, reason: "bye {{ steps.a.output }}"}}
  - {id: slow, shell: hang}
  - {id: never, needs: [halt], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    with anyio.fail_after(5), pytest.raises(RunStopped) as info:
        await run_graph(g.graph, g.scope, g.ctx)
    assert info.value.status == "cancelled"
    assert info.value.reason == "bye a"
    st = harness.statuses(g.run.run_id)
    assert st["halt"] == "succeeded"
    assert st["slow"] == "interrupted"
    assert harness.record(g.run.run_id).steps["slow"].skip_reason == "stopped"
    assert st["never"] == "skipped"


async def test_outer_cancellation_marks_running_interrupted(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: hang}
  - {id: b, needs: [a], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    with anyio.move_on_after(0.1) as scope:
        await run_graph(g.graph, g.scope, g.ctx)
    assert scope.cancelled_caught
    rec = harness.record(g.run.run_id).steps["a"]
    assert rec.status is StepStatus.INTERRUPTED
    assert rec.skip_reason == "interrupted"
    assert "b" not in harness.record(g.run.run_id).steps  # never started: stays pending
    finished = [e for e in harness.events(EventType.STEP_FINISHED) if e.step_path == "a"]
    assert finished and finished[0].data["status"] == "interrupted"


async def test_executor_exception_becomes_failed_step(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: a, shell: boom}\n  - {id: b, needs: [a], shell: ok}"))
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    rec = outcomes["a"].record
    assert rec.status is StepStatus.FAILED
    assert rec.error is not None and "kaboom" in rec.error.message
    assert outcomes["b"].record.skip_reason == "upstream_failed"


async def test_retry_policy_and_timeout(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: flaky, shell: transient, retry: {attempts: 3, delay: 0.01}}
  - {id: slow, shell: "sleep:5", timeout: 0.05}
  - {id: slow_all, shell: "sleep:5", timeout: 0.05, retry: {attempts: 2, delay: 0.01, on_error: all}}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["flaky"].record.attempts == 3
    assert outcomes["flaky"].record.status is StepStatus.FAILED
    retries = harness.events(EventType.STEP_RETRY)
    assert [e.data["attempt"] for e in retries if e.step_path == "flaky"] == [2, 3]
    slow = outcomes["slow"].record
    assert slow.status is StepStatus.FAILED and slow.error and slow.error.type == "timeout"
    assert slow.attempts == 1  # timeouts are not transient by default
    assert outcomes["slow_all"].record.attempts == 2  # on_error: all retries timeouts
