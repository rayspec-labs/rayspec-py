"""The consumer-side view of the policy file: only ``budget`` and ``max_concurrent_runs``."""

from __future__ import annotations

from pathlib import Path

from rayspec.limits import LimitsPolicy, limits_policy
from rayspec.limits.policy import budget_envelope, concurrency_limits, policy_view


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


def test_nonsense_values_are_ignored_rather_than_crashing_a_run() -> None:
    envelope = budget_envelope({"per_run": "lots", "per_day": 0, "per_month": -1})
    assert envelope.active is False
    assert concurrency_limits("many") == {}
    assert concurrency_limits({"claude": 0, "codex": "two", "stub": 2}) == {"stub": 2}


def test_the_failure_cap_may_also_sit_inside_budget() -> None:
    assert budget_envelope({"max_consecutive_failures": 4}).max_consecutive_failures == 4
    assert budget_envelope({"max_consecutive_failures": 4}, 2).max_consecutive_failures == 2
