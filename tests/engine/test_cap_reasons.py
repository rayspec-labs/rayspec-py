# SPDX-License-Identifier: Apache-2.0
"""Which run-level cap tripped the circuit breaker.

`budget_usd`, `max_tokens` and `timeout_total` are one breaker with one `skip_reason`
(`budget_exceeded`), so the reason text is the only place that says which cap actually fired.
It has to name every cap the run is over, in an order that is written down rather than merely
deterministic.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rayspec.engine.context import (
    CAP_KNOBS,
    cap_reasons,
    is_cap_reason,
    utcnow,
)
from rayspec.providers.base import Usage
from rayspec.schema import Defaults

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


def usage(total: int) -> Usage:
    return Usage(input=total)


def test_knob_order_is_the_documented_precedence() -> None:
    assert CAP_KNOBS == ("defaults.budget_usd", "defaults.max_tokens", "defaults.timeout_total")


def test_no_cap_over_is_no_breach() -> None:
    defaults = Defaults.model_validate({"max_tokens": 100, "timeout_total": "1h"})
    assert cap_reasons(usage(10), None, "none", 5.0, defaults) == ()


def test_the_wall_clock_alone_names_only_the_wall_clock() -> None:
    defaults = Defaults.model_validate({"max_tokens": 100, "timeout_total": 60})
    breaches = cap_reasons(usage(10), None, "none", 61.0, defaults)
    assert [b.knobs for b in breaches] == [("defaults.timeout_total",)]
    assert breaches[0].reason.startswith("time limit exceeded (elapsed ")
    assert "timeout_total 1m" in breaches[0].reason


def test_every_cap_that_is_over_is_named() -> None:
    """The losing cap used to be dropped: money won and the clock was never mentioned."""
    defaults = Defaults.model_validate({"budget_usd": 1, "max_tokens": 100, "timeout_total": 60})
    breaches = cap_reasons(usage(500), 2.0, "provider", 61.0, defaults)
    assert [b.knobs for b in breaches] == [
        ("defaults.budget_usd", "defaults.max_tokens"),
        ("defaults.timeout_total",),
    ]
    assert breaches[0].reason == (
        "budget exceeded (cost $2.000 > budget_usd $1.000, tokens 500 > max_tokens 100)"
    )
    assert breaches[1].reason.startswith("time limit exceeded (")


def test_is_cap_reason_recognises_what_the_breaker_writes() -> None:
    defaults = Defaults.model_validate({"budget_usd": 1, "timeout_total": 60})
    breaches = cap_reasons(usage(1), 2.0, "provider", 61.0, defaults)
    joined = "; ".join(b.reason for b in breaches)
    assert is_cap_reason(joined)
    assert not is_cap_reason("step 'a' failed: exit code 1")
    assert not is_cap_reason(None)


async def test_check_budget_reports_both_caps_and_both_knobs(harness: Harness) -> None:
    """A run that is over the token cap AND the clock says so once, naming both knobs."""
    harness.workflow(
        "t",
        "rayspec: 1\nname: t\ndefaults:\n  max_tokens: 1\n  timeout_total: 1\n"
        "steps:\n  - {id: a, shell: ok}\n",
    )
    g = make_graph_harness(harness, harness.load("t"))
    g.run.started_at = utcnow() - timedelta(seconds=120)
    record = g.ctx.new_record(g.graph.step("a"), g.scope)
    record.usage = usage(50)
    g.run.steps[record.path] = record
    g.ctx.accounted_paths.add(record.path)

    reason = await g.ctx.check_budget()
    assert reason is not None
    assert "tokens 50 > max_tokens 1" in reason
    assert "time limit exceeded" in reason and "timeout_total 1.0s" in reason
    warning = harness.events()[-1].data["message"]
    assert "defaults.max_tokens" in warning and "defaults.timeout_total" in warning
    assert "defaults.budget_usd" not in warning  # no cap set, so nothing to raise
