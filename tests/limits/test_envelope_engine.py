"""The spending envelope in a real run: it PAUSES, it drains, and the run resumes cleanly."""

from __future__ import annotations

import anyio
import pytest

from rayspec.engine.context import BUDGET_SKIP_REASON
from rayspec.engine.runtime import EXIT_PAUSED
from rayspec.events.model import EventType
from rayspec.limits import BudgetEnvelope, RunEnvelope, SpendLedger, ledger_path
from rayspec.providers.pricing import PriceTable
from rayspec.providers.stub import StubProvider
from rayspec.schema import RunStatus
from rayspec.store.model import Decision

from .conftest import Project

pytestmark = pytest.mark.anyio

CHAIN = """
rayspec: 1
name: t
agents:
  worker: {provider: claude, model: m1}
steps:
  - {id: a, prompt: "one", agent: worker}
  - {id: b, needs: [a], prompt: "two", agent: worker}
  - {id: c, needs: [b], prompt: "three", agent: worker}
"""

#: $0.002 per step: 1000 input tokens at $2 per million.
PRICES = {"m1": {"input": 2.0, "cached_input": 0, "output": 0}}


def stub() -> StubProvider:
    return StubProvider(script={"defaults": {"usage": {"input": 1000, "output": 0}}})


def envelope(project: Project, run_id: str, **caps: float | int) -> RunEnvelope:
    return RunEnvelope(
        BudgetEnvelope(**caps),  # type: ignore[arg-type]
        SpendLedger(ledger_path(project.home / "projects" / "local-test")),
        run_id=run_id,
    )


async def run_with(project: Project, env: RunEnvelope, **kw: object) -> object:
    runner = project.runner("t", providers={"claude": stub()}, envelope=env, **kw)  # type: ignore[arg-type]
    runner.price_table = PriceTable.from_config(PRICES)
    return await runner.run()


async def test_a_daily_envelope_pauses_the_run_and_it_resumes_cleanly(project: Project) -> None:
    project.workflow("t", CHAIN)
    ledger = SpendLedger(ledger_path(project.home / "projects" / "local-test"))
    ledger.commit("earlier-run", 0.0035)  # earlier spending, same day

    result = await run_with(project, envelope(project, "r1", per_day=0.005), run_id="r1")
    assert result.status is RunStatus.PAUSED  # type: ignore[attr-defined]
    assert result.exit_code == EXIT_PAUSED  # type: ignore[attr-defined]
    reason = result.reason  # type: ignore[attr-defined]
    assert "spending envelope reached" in reason and "budget.per_day" in reason

    record = project.record("r1")
    assert record.pause is not None
    assert record.pause.reason == "budget"
    assert record.pause.message == reason
    assert record.pause.step == "a"  # where the run stopped
    assert project.statuses("r1") == {"a": "succeeded", "b": "skipped", "c": "skipped"}
    assert record.steps["b"].skip_reason == BUDGET_SKIP_REASON
    paused = project.events(EventType.RUN_PAUSED)
    assert paused and paused[0].data["reason"] == "budget"
    # the run's own spend landed in the ledger, and the pause quotes THAT figure
    assert ledger.read().day_usd == pytest.approx(0.0055)
    assert f"${ledger.read().day_usd:.3f}" in reason

    # the ceiling is raised and the run continues where it stopped
    project.sink.clear()
    resumed = await run_with(project, envelope(project, "r1", per_day=1.0), resume="r1")
    assert resumed.status is RunStatus.SUCCEEDED, resumed.reason  # type: ignore[attr-defined]
    assert project.statuses("r1") == {"a": "succeeded", "b": "succeeded", "c": "succeeded"}
    assert project.record("r1").pause is None


async def test_approving_a_paused_run_waives_the_ceiling_for_that_run(project: Project) -> None:
    project.workflow("t", CHAIN)
    result = await run_with(project, envelope(project, "r1", per_run=0.001), run_id="r1")
    assert result.status is RunStatus.PAUSED  # type: ignore[attr-defined]

    record = project.record("r1")
    assert record.pause is not None
    record.pause.decision = Decision(approved=True, comment="looked, it is fine")
    project.store.save(record)

    project.sink.clear()
    env = envelope(project, "r1", per_run=0.001)
    resumed = await run_with(project, env, resume="r1")
    assert resumed.status is RunStatus.SUCCEEDED, resumed.reason  # type: ignore[attr-defined]
    assert env.waived is True
    assert project.record("r1").pause is None


async def test_rejecting_leaves_the_run_paused_on_the_same_ceiling(project: Project) -> None:
    project.workflow("t", CHAIN)
    await run_with(project, envelope(project, "r1", per_run=0.001), run_id="r1")
    record = project.record("r1")
    assert record.pause is not None
    record.pause.decision = Decision(approved=False, comment="no")
    project.store.save(record)

    project.sink.clear()
    resumed = await run_with(project, envelope(project, "r1", per_run=0.001), resume="r1")
    assert resumed.status is RunStatus.PAUSED  # type: ignore[attr-defined]
    assert project.record("r1").pause is not None


CLEANUP = """
rayspec: 1
name: t
agents:
  worker: {provider: claude, model: m1}
steps:
  - {id: a, prompt: "one", agent: worker}
  - {id: b, needs: [a], join: always, prompt: "two", agent: worker}
  - {id: c, needs: [b], join: always, shell: "echo done"}
"""


async def test_join_always_may_still_run_but_may_not_spend(project: Project) -> None:
    """`policy.budget` is the operator's ceiling over the author — the author must not be able
    to opt out of it with four characters of YAML."""
    project.workflow("t", CLEANUP)
    ledger = SpendLedger(ledger_path(project.home / "projects" / "local-test"))

    result = await run_with(project, envelope(project, "r1", per_day=0.001), run_id="r1")
    assert result.status is RunStatus.PAUSED  # type: ignore[attr-defined]
    statuses = project.statuses("r1")
    assert statuses["a"] == "succeeded"
    assert statuses["b"] == "skipped"  # no further agent turn, join: always or not
    assert statuses["c"] == "succeeded"  # the cleanup shell step still runs
    assert ledger.read().day_usd == pytest.approx(0.002)  # one step's worth, not three


async def test_the_failure_breaker_pauses_before_anything_runs(project: Project) -> None:
    project.workflow("t", CHAIN)
    ledger = SpendLedger(ledger_path(project.home / "projects" / "local-test"))
    ledger.record_outcome(failed=True)
    ledger.record_outcome(failed=True)

    result = await run_with(
        project, envelope(project, "r1", max_consecutive_failures=2), run_id="r1"
    )
    assert result.status is RunStatus.PAUSED  # type: ignore[attr-defined]
    assert "circuit breaker open" in result.reason  # type: ignore[attr-defined]
    record = project.record("r1")
    assert record.pause is not None
    assert record.pause.step == "<run>"
    # not a money problem: the pause names the control that actually fired
    assert record.pause.reason == "failures"
    assert set(project.statuses("r1").values()) == {"skipped"}


async def test_approving_a_spend_does_not_clear_the_failure_streak(project: Project) -> None:
    project.workflow("t", CHAIN)
    ledger = SpendLedger(ledger_path(project.home / "projects" / "local-test"))
    ledger.record_outcome(failed=True)
    ledger.record_outcome(failed=True)

    await run_with(
        project, envelope(project, "r1", per_run=0.001, max_consecutive_failures=9), run_id="r1"
    )
    record = project.record("r1")
    assert record.pause is not None and record.pause.reason == "budget"
    record.pause.decision = Decision(approved=True, comment="fine")
    project.store.save(record)

    project.sink.clear()
    env = envelope(project, "r1", per_run=0.001, max_consecutive_failures=9)
    await run_with(project, env, resume="r1")
    # the approval waived the money ceiling and said so; the breaker was never its business
    # (this run then SUCCEEDS, which is what closes the breaker — an outcome, not a waiver)
    messages = [e.data.get("message", "") for e in project.events(EventType.WARNING)]
    assert any("failure breaker is not" in m for m in messages), messages


async def test_a_successful_run_closes_the_breaker_and_a_failed_one_opens_it(
    project: Project,
) -> None:
    project.workflow("t", CHAIN)
    ledger = SpendLedger(ledger_path(project.home / "projects" / "local-test"))
    ledger.record_outcome(failed=True)
    await run_with(project, envelope(project, "r1", max_consecutive_failures=5), run_id="r1")
    assert ledger.read().consecutive_failures == 0

    project.workflow("f", "rayspec: 1\nname: f\nsteps:\n  - {id: a, shell: 'exit 3'}\n")
    runner = project.runner(
        "f",
        providers={"claude": stub()},
        envelope=envelope(project, "r2", max_consecutive_failures=5),
        run_id="r2",
    )
    result = await runner.run()
    assert result.status is RunStatus.FAILED
    assert ledger.read().consecutive_failures == 1


async def test_a_dry_run_never_touches_the_ledger(project: Project) -> None:
    from rayspec.engine.context import RunOptions

    project.workflow("t", CHAIN)
    ledger = SpendLedger(ledger_path(project.home / "projects" / "local-test"))
    runner = project.runner(
        "t",
        providers={"claude": stub()},
        options=RunOptions(dry_run=True),
        envelope=envelope(project, "r1", per_run=0.0000001),
        run_id="r1",
    )
    runner.price_table = PriceTable.from_config(PRICES)
    result = await runner.run()
    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert ledger.read().day_usd == 0.0


async def test_two_runs_finishing_at_once_both_land_in_the_ledger(project: Project) -> None:
    """Real concurrency: two runs of the same project share one ledger and neither is lost."""
    project.workflow("t", CHAIN)
    ledger = SpendLedger(ledger_path(project.home / "projects" / "local-test"))
    results: list[object] = []

    async def one(run_id: str) -> None:
        runner = project.runner(
            "t",
            providers={"claude": stub()},
            envelope=envelope(project, run_id, per_day=10.0),
            run_id=run_id,
        )
        runner.price_table = PriceTable.from_config(PRICES)
        results.append(await runner.run())

    async with anyio.create_task_group() as tg:
        tg.start_soon(one, "ra")
        tg.start_soon(one, "rb")
    assert all(r.status is RunStatus.SUCCEEDED for r in results)  # type: ignore[attr-defined]
    assert ledger.read().day_usd == pytest.approx(0.012)  # 2 runs, 3 steps each, at $0.002
