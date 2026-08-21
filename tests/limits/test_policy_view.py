"""The consumer-side view of the policy file: only ``budget`` and ``max_concurrent_runs``."""

from __future__ import annotations

from pathlib import Path

from rayspec.limits import BudgetEnvelope, LimitsPolicy, SpendState, limits_policy
from rayspec.limits.envelope import envelope_reason, failure_breaker_reason
from rayspec.limits.policy import (
    budget_envelope,
    concurrency_limits,
    limits_for,
    policy_view,
)


def test_a_missing_policy_layer_means_no_limits(tmp_path: Path) -> None:
    assert limits_policy(tmp_path, home=tmp_path).active is False


def test_policy_view_reads_a_mapping_or_an_object() -> None:
    view = policy_view(
        {
            "budget": {"per_run": 1.0, "per_day": "2.5", "per_month": 10},
            "max_consecutive_failures": 3,
            "max_concurrent_runs": {"claude": 2},
        }
    )
    assert view.budget.per_run == 1.0 and view.budget.per_day == 2.5
    assert view.budget.per_month == 10.0 and view.budget.max_consecutive_failures == 3
    assert view.max_concurrent_runs == {"claude": 2}

    class Policy:
        budget = {"per_day": 4.0}
        max_consecutive_failures = None
        max_concurrent_runs = 3

    assert policy_view(Policy()).max_concurrent_runs == {"*": 3}
    assert policy_view(None) == LimitsPolicy()


def test_nonsense_values_are_reported_rather_than_crashing_a_run() -> None:
    problems: list[str] = []
    envelope = budget_envelope({"per_run": "lots", "per_month": -1}, problems=problems)
    assert envelope.active is False
    assert len(problems) == 2
    assert "budget.per_run" in problems[0] and "'lots'" in problems[0]
    assert "budget.per_month" in problems[1]

    problems = []
    assert concurrency_limits("many", problems=problems) == {}
    assert concurrency_limits({"codex": "two", "stub": 2}, problems=problems) == {"stub": 2}
    assert len(problems) == 2
    assert all("max_concurrent_runs" in p for p in problems)


def test_zero_is_the_strictest_ceiling_not_the_absence_of_one() -> None:
    """``0`` is what an operator writes to freeze spending — never "unlimited"."""
    envelope = budget_envelope({"per_day": 0, "per_run": 0.0}, 0)
    assert envelope.per_day == 0.0 and envelope.per_run == 0.0
    assert envelope.max_consecutive_failures == 0
    assert envelope.active is True and envelope.spends is True
    assert concurrency_limits({"claude": 0, "stub": 2}) == {"claude": 0, "stub": 2}
    view = policy_view({"budget": {"per_day": 0}, "max_concurrent_runs": {"stub": 0}})
    assert view.active is True
    assert limits_for(view.max_concurrent_runs, ["stub"]) == {"stub": 0}


def test_a_zero_ceiling_trips_before_anything_is_spent() -> None:
    assert envelope_reason(BudgetEnvelope(per_day=0.0), SpendState(day_usd=0.0), 0.0) is not None
    assert failure_breaker_reason(BudgetEnvelope(max_consecutive_failures=0), SpendState())


def test_the_two_coercions_accept_the_same_spellings() -> None:
    """A quoted number and an integral float are the same ceiling in either place."""
    envelope = budget_envelope({"per_day": "20"}, "3")
    assert envelope.per_day == 20.0 and envelope.max_consecutive_failures == 3
    assert concurrency_limits({"claude": 2.0, "codex": "1"}) == {"claude": 2, "codex": 1}
    problems: list[str] = []
    assert concurrency_limits({"claude": 1.5}, problems=problems) == {}
    assert "whole number" in problems[0]


def test_the_failure_cap_may_also_sit_inside_budget() -> None:
    assert budget_envelope({"max_consecutive_failures": 4}).max_consecutive_failures == 4
    assert budget_envelope({"max_consecutive_failures": 4}, 2).max_consecutive_failures == 2
