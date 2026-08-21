# SPDX-License-Identifier: Apache-2.0
"""The join truth table, checked over random DAGs under every failure policy.

``join: always`` is the finally idiom, so it is the row that matters most and the one that is
easiest to lose: the table has to hold while a graph is draining, while it is being torn down by
fail-fast and after a ``stop:`` cancelled it. A hand-picked example only covers the shape it was
written for, so the property test below generates DAGs, gives them random outcomes and checks
every step against the table as the run actually recorded it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import anyio
import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.errors import RunStopped
from rayspec.engine.graph import classify
from rayspec.engine.scheduler import run_graph
from rayspec.schema import StepStatus
from rayspec.store.model import StepRecord

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio

#: Statuses a step can only have because it was actually dispatched.
RAN = frozenset({StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.INTERRUPTED})
#: ``skip_reason`` values that mean "the graph ended, not this step's needs".
DRAIN_REASONS = frozenset({"run_failed", "stopped"})


def wf(steps: str, **defaults: object) -> str:
    extra = "\n".join(f"  {k}: {v}" for k, v in defaults.items())
    block = f"defaults:\n{extra}\n" if defaults else ""
    return f"rayspec: 1\nname: t\n{block}steps:\n{steps}"


# --------------------------------------------------------------------------------------------------
# the two shapes that were wrong
# --------------------------------------------------------------------------------------------------


async def test_fail_fast_still_runs_join_always(harness: Harness) -> None:
    """Tearing the graph down must not swallow the finally step.

    Two siblings run while ``bad`` fails, so fail-fast cancels the task group — the path that
    used to blanket-skip every pending step, ``join: always`` included.
    """
    harness.workflow(
        "t",
        wf("""
  - {id: warmup, shell: "sleep:0.01"}
  - {id: bad, needs: [warmup], shell: fail}
  - {id: slow, shell: hang}
  - {id: slower, shell: hang}
  - {id: cleanup, needs: [bad], join: always, shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(fail_fast=True))
    with anyio.fail_after(5):
        outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["bad"].record.status is StepStatus.FAILED
    assert outcomes["slow"].record.status is StepStatus.INTERRUPTED
    assert outcomes["cleanup"].record.status is StepStatus.SUCCEEDED
    assert "cleanup" in g.leaf.started
    assert harness.statuses(g.run.run_id)["cleanup"] == "succeeded"


async def test_stop_still_runs_join_always(harness: Harness) -> None:
    """A cancelled run reaches its cleanup step — the table's last row says ``always`` runs."""
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: ok}
  - {id: halt, needs: [a], stop: {status: cancelled, reason: bye}}
  - {id: slow, shell: hang}
  - {id: never, needs: [halt], shell: ok}
  - {id: cleanup, needs: [halt], join: always, shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"))
    with anyio.fail_after(5), pytest.raises(RunStopped):
        await run_graph(g.graph, g.scope, g.ctx)
    statuses = harness.statuses(g.run.run_id)
    assert statuses["cleanup"] == "succeeded"
    assert statuses["never"] == "skipped"
    assert harness.record(g.run.run_id).steps["never"].skip_reason == "stopped"


async def test_teardown_skip_names_the_failed_need(harness: Harness) -> None:
    """A pending step whose need failed is ``upstream_failed`` under fail-fast too.

    Under ``drain`` the join table decides it; the teardown path must not relabel the same
    verdict as ``run_failed``, which says nothing about why. A step that only became ready
    because the cleanup step ran keeps ``run_failed``: nothing about its needs explains it.
    """
    harness.workflow(
        "t",
        wf("""
  - {id: warmup, shell: "sleep:0.01"}
  - {id: bad, needs: [warmup], shell: fail}
  - {id: slow, shell: hang}
  - {id: after, needs: [bad], shell: ok}
  - {id: cleanup, needs: [bad], join: always, shell: ok}
  - {id: post, needs: [cleanup], shell: ok}
"""),
    )
    g = make_graph_harness(harness, harness.load("t"), options=RunOptions(fail_fast=True))
    with anyio.fail_after(5):
        outcomes = await run_graph(g.graph, g.scope, g.ctx)
    assert outcomes["after"].record.skip_reason == "upstream_failed"
    assert outcomes["cleanup"].record.status is StepStatus.SUCCEEDED
    assert outcomes["post"].record.skip_reason == "run_failed"


# --------------------------------------------------------------------------------------------------
# the property test
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One generated step: id, needs, join, body (or a ``stop:``), optional ``when: false``."""

    id: str
    needs: tuple[str, ...]
    join: str
    body: str
    stop: bool = False
    when_false: bool = False

    def yaml(self) -> str:
        parts = [f"id: {self.id}"]
        if self.needs:
            parts.append(f"needs: [{', '.join(self.needs)}]")
        if self.join != "all":
            parts.append(f"join: {self.join}")
        if self.when_false:
            parts.append('when: "false"')
        parts.append("stop: {status: cancelled}" if self.stop else f"shell: {self.body!r}")
        return "  - {" + ", ".join(parts) + "}"


def random_dag(rng: random.Random, *, with_stop: bool) -> list[Step]:
    """A random DAG of 4 to 8 steps, declared in a shuffled (non-topological) order."""
    size = rng.randint(4, 8)
    steps: list[Step] = []
    for index in range(size):
        earlier = [s.id for s in steps]
        pool = rng.sample(earlier, k=rng.randint(0, min(2, len(earlier))))
        needs = tuple(sorted(pool))
        steps.append(
            Step(
                id=f"s{index}",
                needs=needs,
                join=rng.choice(["all", "any", "always"]) if needs else "all",
                body=rng.choice(["ok", "ok", "fail", "sleep:0.01", "sleep:0.02"]),
                when_false=not needs and rng.random() < 0.25,
            )
        )
    if with_stop:
        candidates = [i for i, s in enumerate(steps) if s.needs and not s.when_false]
        if candidates:
            index = rng.choice(candidates)
            steps[index] = Step(
                id=steps[index].id,
                needs=steps[index].needs,
                join=steps[index].join,
                body="ok",
                stop=True,
            )
    rng.shuffle(steps)
    return steps


def expected(step: Step, needs: list[StepRecord]) -> tuple[str, str | None]:
    """The join table's verdict for ``step``: ``("run"|"skip"|"drain", skip_reason)``.

    ``drain`` is the ambiguous cell — the row that runs unless the graph happened to be
    draining by the time the step became ready, which no recorded outcome can pin down.
    """
    classes = [classify(rec) for rec in needs]
    if step.join == "always":
        return "run", None
    if any(c == "failed" for c in classes):
        return "skip", "upstream_failed"
    if classes and all(c == "skipped" for c in classes):
        return "skip", "upstream_skipped"
    if step.join == "all" and any(c == "skipped" for c in classes):
        return "skip", "upstream_skipped"
    return "drain", None


def check_case(steps: list[Step], records: dict[str, StepRecord], *, cancelled: bool) -> int:
    """Assert the table for every step; returns how many ``join: always`` rows were checked.

    After a cancellation the leftovers are labelled ``stopped`` rather than by the need that
    explains them — nothing on those branches failed — so that reason is accepted for any skip.
    """
    always_checked = 0
    for step in steps:
        record = records.get(step.id)
        assert record is not None, f"{step.id} was never decided"
        needs = [records[n] for n in step.needs]
        assert all(r is not None for r in needs)
        verdict, reason = expected(step, needs)
        where = f"{step.id} (join {step.join}, needs {list(step.needs)})"
        if verdict == "skip":
            assert record.status is StepStatus.SKIPPED, f"{where}: expected skip, got {record}"
            allowed = {reason, "stopped"} if cancelled else {reason}
            assert record.skip_reason in allowed, where
            continue
        if step.when_false:
            assert record.status is StepStatus.SKIPPED, where
            assert record.skip_reason == "when_false" or (
                verdict == "drain" and record.skip_reason in DRAIN_REASONS
            ), where
            continue
        if verdict == "run":
            always_checked += 1
            assert record.status in RAN, f"{where}: join always must run, got {record}"
            continue
        assert record.status in RAN or record.skip_reason in DRAIN_REASONS, where
    return always_checked


MODES = ["drain", "fail_fast", "continue", "stop"]


@pytest.mark.parametrize("mode", MODES)
async def test_join_table_holds_over_random_dags(harness: Harness, mode: str) -> None:
    """Every row of the join table, over 25 generated DAGs per failure policy."""
    always_seen = 0
    for seed in range(25):
        rng = random.Random(f"{mode}-{seed}")
        steps = random_dag(rng, with_stop=mode == "stop")
        defaults = {"on_step_failure": "continue"} if mode == "continue" else {}
        harness.workflow("t", wf("\n".join(s.yaml() for s in steps), **defaults))
        options = RunOptions(fail_fast=mode == "fail_fast")
        g = make_graph_harness(harness, harness.load("t"), options=options)
        cancelled = False
        with anyio.fail_after(10):
            try:
                await run_graph(g.graph, g.scope, g.ctx)
            except RunStopped:
                # a generated ``stop:`` step may itself be skipped by its needs, so whether the
                # signal fires is part of what is generated
                assert mode == "stop"
                cancelled = True
        records = harness.record(g.run.run_id).steps
        always_seen += check_case(steps, records, cancelled=cancelled)
    assert always_seen > 0, "the generator produced no join: always row to check"
