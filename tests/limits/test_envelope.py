"""The spending envelope and the consecutive-failure breaker (pure logic + the run view)."""

from __future__ import annotations

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
    ledger.commit("earlier", 0.9)
    env = RunEnvelope(BudgetEnvelope(per_day=1.0), ledger, run_id="mine")
    assert env.check(0.05) is None
    reason = env.check(0.2)  # 0.9 + 0.2 = 1.1 > 1.0
    assert reason is not None and "today" in reason
    assert ledger.read().day_usd == pytest.approx(1.1)


def test_the_pause_kind_names_the_control_that_fired(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    money = RunEnvelope(BudgetEnvelope(per_day=0.01), ledger, run_id="m")
    assert money.check(5.0) is not None and money.pause_kind == "budget"

    ledger.record_outcome(failed=True)
    breaker = RunEnvelope(BudgetEnvelope(max_consecutive_failures=1), ledger, run_id="b")
    assert breaker.check(0.0) is not None and breaker.pause_kind == "failures"


def test_waiving_a_money_pause_leaves_the_failure_breaker_alone(tmp_path: Path) -> None:
    """Two independent guardrails: saying yes to one dollar figure is not saying yes to the
    other control — and a control that is still armed still stops the run."""
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.record_outcome(failed=True)
    ledger.record_outcome(failed=True)
    env = RunEnvelope(
        BudgetEnvelope(per_day=0.01, max_consecutive_failures=2), ledger, run_id="mine"
    )
    assert env.check(5.0) is not None
    env.waive()
    assert env.waived_spend is True and env.waived_failures is False
    assert env.active is True  # the breaker was never answered
    reason = env.check(5.0)
    assert reason is not None and "circuit breaker open" in reason
    assert ledger.read().consecutive_failures == 2  # untouched


def test_waiving_a_money_pause_still_counts_what_the_run_spends(tmp_path: Path) -> None:
    """A waiver says "this run may cost more", not "this run costs nothing" — the next run's
    day total has to include it."""
    ledger = SpendLedger(ledger_path(tmp_path))
    env = RunEnvelope(
        BudgetEnvelope(per_day=0.01, max_consecutive_failures=9), ledger, run_id="mine"
    )
    assert env.check(5.0) is not None
    env.waive()
    assert env.check(7.0) is None  # the money ceiling no longer stops it
    assert ledger.read().day_usd == pytest.approx(7.0)  # but it is still the operator's money


def test_a_run_with_nothing_left_to_enforce_still_records_its_final_total(
    tmp_path: Path,
) -> None:
    """With every control answered there is nothing to ask per step, so the per-step commit is
    skipped — but the run's own total is committed when it finishes, the same as any other."""
    ledger = SpendLedger(ledger_path(tmp_path))
    env = RunEnvelope(BudgetEnvelope(per_day=0.01), ledger, run_id="mine")
    env.waive()
    assert env.active is False
    assert env.check(7.0) is None
    env.commit_final(7.0)
    assert ledger.read().day_usd == pytest.approx(7.0)


def test_closing_the_breaker_does_not_waive_the_spending_envelope(tmp_path: Path) -> None:
    """An operator answering a question about flakiness is not being asked to give up their
    money ceiling, and must not lose one they were never asked about."""
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.record_outcome(failed=True)
    env = RunEnvelope(
        BudgetEnvelope(per_day=0.01, max_consecutive_failures=1), ledger, run_id="mine"
    )
    assert env.check(5.0) is not None and env.pause_kind == "failures"

    env.waive(close_breaker=True)  # the breaker's OWN pause was approved
    assert env.waived_failures is True and env.waived_spend is False
    assert ledger.read().consecutive_failures == 0  # the streak is forgiven
    assert env.active is True

    reason = env.check(5.0)
    assert reason is not None and "budget.per_day" in reason
    assert env.pause_kind == "budget"


def test_waiving_both_controls_leaves_nothing_to_stop_the_run(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    ledger.record_outcome(failed=True)
    env = RunEnvelope(
        BudgetEnvelope(per_day=0.01, max_consecutive_failures=1), ledger, run_id="mine"
    )
    env.waive()
    env.waive(close_breaker=True)
    assert env.active is False
    assert env.check(5.0) is None


def test_a_waiver_of_a_control_that_is_not_set_changes_nothing(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    env = RunEnvelope(BudgetEnvelope(per_day=0.01), ledger, run_id="mine")
    env.waive(close_breaker=True)  # no breaker configured: nothing to close, nothing to reset
    assert env.active is True
    assert env.check(5.0) is not None


def test_settle_rephrases_the_ceiling_from_the_final_total(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    env = RunEnvelope(BudgetEnvelope(per_day=0.01), ledger, run_id="mine")
    first = env.check(0.02)
    final = env.settle(0.10)
    assert first is not None and final is not None
    assert "$0.020" in first and "$0.100" in final


def test_outcomes_only_touch_the_counter_when_it_is_configured(tmp_path: Path) -> None:
    ledger = SpendLedger(ledger_path(tmp_path))
    RunEnvelope(BudgetEnvelope(per_day=1.0), ledger, run_id="m").record_outcome(failed=True)
    assert ledger.read().consecutive_failures == 0
    RunEnvelope(BudgetEnvelope(max_consecutive_failures=1), ledger, run_id="m").record_outcome(
        failed=True
    )
    assert ledger.read().consecutive_failures == 1


def test_the_ledgers_own_warnings_reach_the_caller(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("nonsense", encoding="utf-8")
    env = RunEnvelope(BudgetEnvelope(per_day=1.0), SpendLedger(path), run_id="m")
    env.check(0.5)
    assert any("spend.json" in w for w in env.take_warnings())
