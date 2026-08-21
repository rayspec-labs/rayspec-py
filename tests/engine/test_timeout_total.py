"""Run-level wall-clock cap: ``defaults.timeout_total``.

Schema (additive, duration parsing), engine enforcement (no new step starts once the cap is
exceeded, running leaves drain, run ``failed`` with reason ``time limit exceeded (…)``, exit 1)
and the resume rule: the clock keeps counting from the ORIGINAL start, not from this attempt.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rayspec.engine.context import BUDGET_SKIP_REASON
from rayspec.events.model import EventType
from rayspec.providers.stub import StubProvider
from rayspec.schema import Defaults, RunStatus, SchemaError, parse_workflow

from .conftest import Harness

pytestmark = pytest.mark.anyio


# -- schema ------------------------------------------------------------------------------------


def test_timeout_total_is_optional_and_parses_durations() -> None:
    assert Defaults().timeout_total is None
    assert Defaults.model_validate({"timeout_total": "30m"}).timeout_total == 1800.0
    assert Defaults.model_validate({"timeout_total": "1h30m"}).timeout_total == 5400.0
    assert Defaults.model_validate({"timeout_total": 90}).timeout_total == 90.0


def test_timeout_total_rejects_nonsense_with_a_hint() -> None:
    doc = {"rayspec": 1, "name": "t", "defaults": {"timeout_total": "soon"}, "steps": []}
    with pytest.raises(SchemaError) as exc:
        parse_workflow(doc)
    assert "defaults.timeout_total" in str(exc.value) and "1h30m" in str(exc.value)
    doc = {"rayspec": 1, "name": "t", "defaults": {"timeout_total": 0}, "steps": []}
    with pytest.raises(SchemaError) as exc:
        parse_workflow(doc)
    assert "defaults.timeout_total" in str(exc.value) and "greater than 0" in str(exc.value)


# -- engine ------------------------------------------------------------------------------------


def wf(defaults: str, steps: str) -> str:
    return f"rayspec: 1\nname: t\ndefaults:\n{defaults}\nsteps:\n{steps}"


CHAIN = """
  - {id: a, prompt: "one"}
  - {id: b, needs: [a], prompt: "two"}
  - {id: c, needs: [b], shell: "echo done"}
"""


async def test_the_clock_trips_the_breaker_and_no_new_step_starts(harness: Harness) -> None:
    harness.workflow("t", wf("  timeout_total: 0.05", CHAIN))
    provider = StubProvider(script={"steps": {"a": {"latency_ms": 120}}})
    result = await harness.run("t", providers={"claude": provider})
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    assert result.reason is not None
    assert result.reason.startswith("time limit exceeded (elapsed ")
    assert "> timeout_total 0.1s)" in result.reason
    assert harness.statuses(result.run_id) == {"a": "succeeded", "b": "skipped", "c": "skipped"}
    run = harness.record(result.run_id)
    assert run.steps["b"].skip_reason == BUDGET_SKIP_REASON
    assert run.reason == result.reason
    warnings = [e.data["message"] for e in harness.events(EventType.WARNING)]
    assert any("time limit exceeded" in w and "defaults.timeout_total" in w for w in warnings)
    assert harness.events(EventType.RUN_FINISHED)[0].data["reason"] == result.reason


async def test_running_leaves_drain_when_the_clock_runs_out(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  timeout_total: 0.05\n  max_parallel: 4",
            """
  - {id: quick, prompt: "quick"}
  - {id: slow, prompt: "slow"}
  - {id: after_quick, needs: [quick], prompt: "never"}
  - {id: after_slow, needs: [slow], shell: "echo never"}
""",
        ),
    )
    provider = StubProvider(
        script={"steps": {"quick": {"latency_ms": 120}, "slow": {"latency_ms": 400}}}
    )
    result = await harness.run("t", providers={"claude": provider})
    assert result.status is RunStatus.FAILED and "timeout_total" in (result.reason or "")
    statuses = harness.statuses(result.run_id)
    # ``slow`` was already running when the cap tripped: it finished (drain, no cancellation)
    assert statuses["quick"] == "succeeded" and statuses["slow"] == "succeeded"
    assert statuses["after_quick"] == "skipped" and statuses["after_slow"] == "skipped"


async def test_no_timeout_total_means_no_clock(harness: Harness) -> None:
    harness.workflow("t", f"rayspec: 1\nname: t\nsteps:\n{CHAIN}")
    result = await harness.run("t", providers={"claude": StubProvider()})
    assert result.status is RunStatus.SUCCEEDED, result.reason


async def test_resume_measures_from_the_original_start(harness: Harness) -> None:
    """The cap is 2h of RUN, not 2h per attempt: a resume keeps counting from ``started_at``."""
    harness.workflow(
        "t",
        wf(
            "  timeout_total: 1h",
            """
  - {id: a, shell: "echo one"}
  - {id: b, needs: [a], shell: "echo two"}
""",
        ),
    )
    first = await harness.run("t")
    assert first.status is RunStatus.SUCCEEDED, first.reason

    # a resume right away is still inside the cap
    harness.sink.clear()
    again = await harness.run("t", resume=first.run_id)
    assert again.status is RunStatus.SUCCEEDED, again.reason
    assert again.reused == ["a", "b"]

    # rewrite the recorded start three hours into the past: the run has spent its hour
    record = harness.store.load(first.run_id)
    assert record.started_at is not None
    record.started_at = record.started_at - timedelta(hours=3)
    harness.store.save(record)

    harness.sink.clear()
    expired = await harness.run("t", resume=first.run_id)
    assert expired.status is RunStatus.FAILED and expired.exit_code == 1
    assert expired.reason is not None
    assert "> timeout_total 1h 0m)" in expired.reason
    assert "elapsed 3h 0m" in expired.reason
    # finished steps are still replayed (a replay is free) — nothing new starts
    assert expired.reused == ["a", "b"]
    assert harness.statuses(first.run_id) == {"a": "succeeded", "b": "succeeded"}


async def test_a_leaf_queued_for_a_slot_does_not_start_after_the_cap(harness: Harness) -> None:
    """The gate is asked again once the permit is held: a queued step is a step that has not
    started, and a wall-clock cap that keeps launching queued work is not a cap."""
    harness.workflow(
        "t",
        wf(
            "  timeout_total: 0.05\n  max_parallel: 1",
            """
  - {id: a, prompt: "one"}
  - {id: b, prompt: "two"}
  - {id: c, prompt: "three"}
  - {id: cleanup, join: always, shell: "echo cleaned"}
""",
        ),
    )
    provider = StubProvider(script={"steps": {"a": {"latency_ms": 120}}})
    result = await harness.run("t", providers={"claude": provider})
    assert result.status is RunStatus.FAILED and "timeout_total" in (result.reason or "")
    statuses = harness.statuses(result.run_id)
    assert statuses["a"] == "succeeded"
    # b and c were ready before the clock ran out and then waited for the single slot
    assert statuses["b"] == "skipped" and statuses["c"] == "skipped"
    run = harness.record(result.run_id)
    assert run.steps["b"].skip_reason == BUDGET_SKIP_REASON
    assert run.steps["b"].attempts == 0  # queued, never attempted
    assert statuses["cleanup"] == "succeeded"  # join: always still drains


async def test_the_tripped_clock_outranks_a_stop_step(harness: Harness) -> None:
    """A ``stop: {status: succeeded}`` reached while draining must not report a capped run as
    successful — the cap decides the run status, and its outputs are not published."""
    harness.workflow(
        "t",
        "rayspec: 1\nname: t\ndefaults:\n  timeout_total: 0.05\n"
        "outputs:\n  done: yes\nsteps:\n"
        '  - {id: slow, prompt: "one"}\n'
        '  - {id: bye, needs: [slow], join: always, stop: {status: succeeded, reason: "early"}}\n',
    )
    provider = StubProvider(script={"steps": {"slow": {"latency_ms": 120}}})
    result = await harness.run("t", providers={"claude": provider})
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    assert "time limit exceeded" in (result.reason or "")
    run = harness.record(result.run_id)
    assert run.status is RunStatus.FAILED and run.reason == result.reason
    assert not run.outputs
