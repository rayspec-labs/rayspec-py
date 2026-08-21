"""The spending envelope and the consecutive-failure breaker (pure logic + the run view)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from rayspec.limits import (
    BudgetEnvelope,
    RunEnvelope,
    SpendLedger,
    SpendState,
    envelope_reason,
    failure_breaker_reason,
    ledger_path,
)

WHEN = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def test_an_empty_envelope_is_inactive() -> None:
    assert BudgetEnvelope().active is False
    assert BudgetEnvelope(per_day=1.0).active is True
    assert BudgetEnvelope(max_consecutive_failures=3).active is True
    assert BudgetEnvelope(max_consecutive_failures=3).spends is False


def test_strictly_greater_trips_and_the_reason_names_the_knob() -> None:
    envelope = BudgetEnvelope(per_day=1.0)
    assert envelope_reason(envelope, SpendState(day_usd=1.0), 1.0) is None
    reason = envelope_reason(envelope, SpendState(day_usd=1.5), 1.5)
    assert reason is not None
    assert "budget.per_day" in reason and "$1.500" in reason and "$1.000" in reason


def test_per_run_beats_per_day_beats_per_month_in_the_message() -> None:
    envelope = BudgetEnvelope(per_run=0.1, per_day=0.2, per_month=0.3)
    state = SpendState(day_usd=9.0, month_usd=9.0)
    assert "budget.per_run" in (envelope_reason(envelope, state, 9.0) or "")
    assert "budget.per_day" in (envelope_reason(envelope, state, 0.0) or "")


def test_an_unknown_run_cost_cannot_trip_a_money_ceiling() -> None:
    envelope = BudgetEnvelope(per_run=0.1)
    assert envelope_reason(envelope, SpendState(), None) is None


def test_the_failure_breaker_opens_at_the_cap() -> None:
    envelope = BudgetEnvelope(max_consecutive_failures=2)
    assert failure_breaker_reason(envelope, SpendState(consecutive_failures=1)) is None
    reason = failure_breaker_reason(envelope, SpendState(consecutive_failures=2))
    assert reason is not None and "max_consecutive_failures 2" in reason


def test_run_envelope_commits_before_it_reads(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.commit("earlier", 0.9, when=WHEN)
    env = RunEnvelope(BudgetEnvelope(per_day=1.0), ledger, run_id="mine", started_at=WHEN)
    assert env.check(0.05) is None
    reason = env.check(0.2)  # 0.9 + 0.2 = 1.1 > 1.0
    assert reason is not None and "today" in reason
    assert ledger.read(when=WHEN).day_usd == pytest.approx(1.1)


def test_a_waived_envelope_stops_stopping_the_run_and_closes_the_breaker(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.record_outcome(failed=True)
    ledger.record_outcome(failed=True)
    env = RunEnvelope(
        BudgetEnvelope(per_day=0.01, max_consecutive_failures=2),
        ledger,
        run_id="mine",
        started_at=WHEN,
    )
    assert env.check(5.0) is not None
    env.waive()
    assert env.active is False
    assert env.check(5.0) is None
    assert ledger.read(when=WHEN).consecutive_failures == 0


def test_outcomes_only_touch_the_counter_when_it_is_configured(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    RunEnvelope(BudgetEnvelope(per_day=1.0), ledger, run_id="m", started_at=WHEN).record_outcome(
        failed=True
    )
    assert ledger.read(when=WHEN).consecutive_failures == 0
    RunEnvelope(
        BudgetEnvelope(max_consecutive_failures=1), ledger, run_id="m", started_at=WHEN
    ).record_outcome(failed=True)
    assert ledger.read(when=WHEN).consecutive_failures == 1
