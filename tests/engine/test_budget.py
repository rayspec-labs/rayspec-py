"""Run-level circuit breaker: ``defaults.budget_usd`` / ``defaults.max_tokens``.

Schema (additive, Duration-like parsing), engine enforcement (no new leaves after the cap is
exceeded, running leaves drain, run ``failed`` with reason ``budget exceeded (…)``, exit 1),
resume after raising the cap, composites, and ``rayspec plan`` showing the caps.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.engine.context import BUDGET_SKIP_REASON, RunOptions
from rayspec.events.model import EventType
from rayspec.providers.pricing import PriceTable
from rayspec.providers.stub import StubProvider
from rayspec.schema import Defaults, RunStatus, SchemaError, StepStatus, parse_workflow
from rayspec.schema.workflow import parse_money, parse_token_count

from .conftest import Harness

pytestmark = pytest.mark.anyio


# -- schema ------------------------------------------------------------------------------------


def test_parse_token_count_accepts_ints_and_k_m_suffixes() -> None:
    assert parse_token_count(1500) == 1500
    assert parse_token_count("1500") == 1500
    assert parse_token_count("500k") == 500_000
    assert parse_token_count("2M") == 2_000_000
    assert parse_token_count("1.5m") == 1_500_000
    assert parse_token_count("1_000_000") == 1_000_000
    for bad in (True, None, 0, -1, "0", "12x", "", "k", 1.5):
        with pytest.raises(ValueError, match=r"token count|must be greater"):
            parse_token_count(bad)
    # a fraction is only meaningful with a k/M unit; never silently rounded
    for fractional in ("1.5", "0.5", "1.0005k"):
        with pytest.raises(ValueError, match=r"whole number"):
            parse_token_count(fractional)


def test_parse_money_accepts_numbers_and_dollar_strings() -> None:
    assert parse_money(1.5) == 1.5
    assert parse_money(2) == 2.0
    assert parse_money("1.50") == 1.5
    assert parse_money("$0.25") == 0.25
    assert parse_money("12 USD") == 12.0
    for bad in (True, None, 0, -0.5, "$", "free", ""):
        with pytest.raises(ValueError, match=r"amount|must be greater"):
            parse_money(bad)


def test_defaults_caps_are_optional_and_validated() -> None:
    assert Defaults().budget_usd is None and Defaults().max_tokens is None
    d = Defaults.model_validate({"budget_usd": "$1.50", "max_tokens": "500k"})
    assert d.budget_usd == 1.5 and d.max_tokens == 500_000
    doc = {"rayspec": 1, "name": "t", "defaults": {"max_tokens": "lots"}, "steps": []}
    with pytest.raises(SchemaError) as exc:
        parse_workflow(doc)
    assert "defaults.max_tokens" in str(exc.value) and "500k" in str(exc.value)
    doc = {"rayspec": 1, "name": "t", "defaults": {"budget_usd": 0}, "steps": []}
    with pytest.raises(SchemaError) as exc:
        parse_workflow(doc)
    assert "defaults.budget_usd" in str(exc.value)


# -- engine ------------------------------------------------------------------------------------


def wf(defaults: str, steps: str) -> str:
    return f"rayspec: 1\nname: t\ndefaults:\n{defaults}\nsteps:\n{steps}"


CHAIN = """
  - {id: a, prompt: "one"}
  - {id: b, needs: [a], prompt: "two"}
  - {id: c, needs: [b], prompt: "three"}
  - {id: d, needs: [c], shell: "echo done"}
"""


def stub(tokens_per_step: int = 1000, **steps: dict) -> StubProvider:
    return StubProvider(
        script={
            "steps": steps,
            "defaults": {"usage": {"input": tokens_per_step, "output": 0}},
        }
    )


async def test_max_tokens_trips_drains_and_fails_the_run(harness: Harness) -> None:
    harness.workflow("t", wf("  max_tokens: 1500", CHAIN))
    result = await harness.run("t", providers={"claude": stub()})
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    assert result.reason == "budget exceeded (tokens 2,000 > max_tokens 1,500)"
    st = harness.statuses(result.run_id)
    assert st == {"a": "succeeded", "b": "succeeded", "c": "skipped", "d": "skipped"}
    run = harness.record(result.run_id)
    assert run.steps["c"].skip_reason == BUDGET_SKIP_REASON
    assert run.steps["d"].skip_reason == BUDGET_SKIP_REASON
    assert run.reason == result.reason and result.usage.total == 2000
    warnings = [e.data["message"] for e in harness.events(EventType.WARNING)]
    assert any("budget exceeded (tokens 2,000 > max_tokens 1,500)" in w for w in warnings)
    assert harness.finished("c").data["skip_reason"] == BUDGET_SKIP_REASON
    assert harness.events(EventType.RUN_FINISHED)[0].data["reason"] == result.reason


async def test_budget_usd_uses_provider_cost_or_pricing_table(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  budget_usd: 0.003",
            """
  - {id: a, prompt: "one", agent: {provider: claude, model: m1}}
  - {id: b, needs: [a], prompt: "two", agent: {provider: claude, model: m1}}
  - {id: c, needs: [b], prompt: "three", agent: {provider: claude, model: m1}}
""",
        ),
    )
    # the stub reports no cost: $0.002 per step comes from the pricing table (1000 tok @ $2/M)
    runner = harness.runner("t", providers={"claude": stub()})
    runner.price_table = PriceTable.from_config(
        {"m1": {"input": 2.0, "cached_input": 0, "output": 0}}
    )
    result = await runner.run()
    assert result.status is RunStatus.FAILED
    assert result.reason == "budget exceeded (cost ~$0.004 > budget_usd $0.003)"
    assert harness.statuses(result.run_id) == {"a": "succeeded", "b": "succeeded", "c": "skipped"}

    # no provider cost and no pricing entry: the cost cap cannot trip (tokens still count)
    harness.sink.clear()
    harness.workflow(
        "p",
        wf(
            "  budget_usd: 0.5",
            """
  - {id: a, prompt: "one"}
  - {id: b, needs: [a], prompt: "two"}
""",
        ),
    )
    priced = StubProvider(
        script={"steps": {"a": {"text": "x", "usage": {"input": 10, "output": 1}}}},
    )
    result = await harness.run("p", providers={"claude": priced})
    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert result.cost_usd is None


async def test_running_leaves_drain_when_the_cap_trips(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  max_tokens: 1500\n  max_parallel: 4",
            """
  - {id: big, prompt: "spend"}
  - {id: slow, prompt: "slow one"}
  - {id: after_big, needs: [big], prompt: "never"}
  - {id: after_slow, needs: [slow], shell: "echo never"}
""",
        ),
    )
    provider = StubProvider(
        script={
            "steps": {
                "big": {"usage": {"input": 2000, "output": 0}},
                "slow": {"latency_ms": 300, "usage": {"input": 10, "output": 0}},
            },
        }
    )
    result = await harness.run("t", providers={"claude": provider})
    assert result.status is RunStatus.FAILED and "max_tokens" in (result.reason or "")
    st = harness.statuses(result.run_id)
    # ``slow`` was already running when ``big`` tripped the cap: it finished (drain)
    assert st["big"] == "succeeded" and st["slow"] == "succeeded"
    assert st["after_big"] == "skipped" and st["after_slow"] == "skipped"
    assert harness.record(result.run_id).steps["after_slow"].skip_reason == BUDGET_SKIP_REASON


async def test_resume_trips_again_until_the_cap_is_raised(harness: Harness) -> None:
    harness.workflow("t", wf("  max_tokens: 1500", CHAIN))
    first = await harness.run("t", providers={"claude": stub()})
    assert first.status is RunStatus.FAILED

    # same cap: the replayed records already exceed it → nothing new runs, failed again
    harness.sink.clear()
    again = await harness.run("t", providers={"claude": stub()}, resume=first.run_id)
    assert again.status is RunStatus.FAILED and "max_tokens 1,500" in (again.reason or "")
    assert again.reused == ["a", "b"]
    assert harness.statuses(first.run_id)["c"] == "skipped"

    # raise the cap (the workflow hash changes → --force), the rest of the run completes
    harness.workflow("t", wf("  max_tokens: 10000", CHAIN))
    harness.sink.clear()
    done = await harness.run(
        "t", providers={"claude": stub()}, resume=first.run_id, options=RunOptions(force=True)
    )
    assert done.status is RunStatus.SUCCEEDED and done.exit_code == 0, done.reason
    assert done.reused == ["a", "b"]
    assert harness.statuses(first.run_id) == {
        "a": "succeeded",
        "b": "succeeded",
        "c": "succeeded",
        "d": "succeeded",
    }
    assert done.usage.total == 3000


async def test_cap_stops_retries_and_loop_iterations(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  max_tokens: 2500",
            """
  - id: build
    loop:
      max_iterations: 10
      steps:
        - {id: think, prompt: "iteration {{ iteration.n }}"}
  - {id: after, needs: [build], shell: "echo x"}
""",
        ),
    )
    result = await harness.run("t", providers={"claude": stub()})
    assert result.status is RunStatus.FAILED and "max_tokens 2,500" in (result.reason or "")
    run = harness.record(result.run_id)
    assert run.steps["build"].status is StepStatus.FAILED
    assert run.steps["build"].loop is not None and run.steps["build"].loop.iterations == 3
    assert run.steps["build"].error is not None and "budget" in run.steps["build"].error.message
    assert run.steps["after"].status is StepStatus.SKIPPED
    assert run.steps["after"].skip_reason == BUDGET_SKIP_REASON
    # three iterations ran (1000 each → 3000 > 2500 after the third), no fourth was started
    assert sorted(p for p in run.steps if p.startswith("build[")) == [
        "build[1]/think",
        "build[2]/think",
        "build[3]/think",
    ]

    # a transient failure is not retried once the cap has tripped (``spend`` trips it while
    # ``flaky``'s first attempt is still in flight)
    harness.sink.clear()
    harness.workflow(
        "r",
        wf(
            "  max_tokens: 500",
            """
  - {id: spend, prompt: "x"}
  - {id: flaky, prompt: "y"}
""",
        ),
    )
    flaky = StubProvider(
        script={
            "steps": {
                "spend": {"usage": {"input": 1000, "output": 0}},
                "flaky": {
                    "latency_ms": 150,
                    "fail": {"kind": "api", "message": "rate limited", "transient": True},
                },
            }
        }
    )
    result = await harness.run("r", providers={"claude": flaky})
    assert result.status is RunStatus.FAILED and "max_tokens 500" in (result.reason or "")
    assert harness.record(result.run_id).steps["flaky"].attempts == 1
    assert harness.events(EventType.STEP_RETRY) == []


async def test_each_items_stop_launching_once_tripped(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  max_tokens: 1500",
            """
  - id: fan
    each: "[1, 2, 3]"
    max_parallel: 1
    steps:
      - {id: work, prompt: "item {{ item }}"}
      - {id: post, needs: [work], shell: "echo {{ item }}"}
""",
        ),
    )
    result = await harness.run("t", providers={"claude": stub()})
    assert result.status is RunStatus.FAILED and "max_tokens 1,500" in (result.reason or "")
    run = harness.record(result.run_id)
    assert run.steps["fan"].status is StepStatus.FAILED
    statuses = {p: r.status.value for p, r in run.steps.items() if p.startswith("fan[")}
    assert statuses["fan[0]/work"] == "succeeded" and statuses["fan[0]/post"] == "succeeded"
    assert statuses["fan[1]/work"] == "succeeded"
    # after the second item's prompt the cap tripped: its shell step and the third item skip
    assert statuses["fan[1]/post"] == "skipped"
    assert statuses.get("fan[2]/work") == "skipped"
    assert run.steps["fan[2]/work"].skip_reason == BUDGET_SKIP_REASON


async def test_no_caps_means_no_breaker(harness: Harness) -> None:
    harness.workflow("t", f"rayspec: 1\nname: t\nsteps:\n{CHAIN}")
    result = await harness.run("t", providers={"claude": stub(10_000_000)})
    assert result.status is RunStatus.SUCCEEDED


DIAMOND = """
  - {id: a, prompt: "root"}
  - {id: b, needs: [a], prompt: "big and slow"}
  - {id: c, needs: [a], prompt: "cheap"}
  - {id: d, needs: [c], prompt: "cheap too"}
  - {id: e, needs: [b, d], prompt: "last"}
"""


def diamond_stub() -> StubProvider:
    return StubProvider(
        script={
            "steps": {
                "a": {"usage": {"input": 500, "output": 0}},
                "b": {"latency_ms": 300, "usage": {"input": 1000, "output": 0}},
                "c": {"usage": {"input": 10, "output": 0}},
                "d": {"usage": {"input": 10, "output": 0}},
                "e": {"usage": {"input": 10, "output": 0}},
            },
        }
    )


async def test_same_cap_resume_replays_finished_steps_instead_of_skipping_them(
    harness: Harness,
) -> None:
    """Review fix: a tripped breaker must not overwrite reusable records with ``skipped``.

    First run: ``a``, ``c``, ``d`` finish while ``b`` is still running; ``b`` trips the cap and
    ``e`` is skipped. A same-cap resume replays ``a``/``b``/``c`` (trips again) — ``d`` was
    pending at that moment and must be REPLAYED (free), not recorded skipped; the raised-cap
    resume then reuses all four and runs only ``e``.
    """
    harness.workflow("t", wf("  max_tokens: 1500", DIAMOND))
    first = await harness.run("t", providers={"claude": diamond_stub()})
    assert first.status is RunStatus.FAILED and "max_tokens 1,500" in (first.reason or "")
    assert harness.statuses(first.run_id) == {
        "a": "succeeded",
        "b": "succeeded",
        "c": "succeeded",
        "d": "succeeded",
        "e": "skipped",
    }

    harness.sink.clear()
    again = await harness.run("t", providers={"claude": diamond_stub()}, resume=first.run_id)
    assert again.status is RunStatus.FAILED and "max_tokens 1,500" in (again.reason or "")
    assert sorted(again.reused) == ["a", "b", "c", "d"]
    assert harness.statuses(first.run_id) == {
        "a": "succeeded",
        "b": "succeeded",
        "c": "succeeded",
        "d": "succeeded",
        "e": "skipped",
    }
    assert harness.record(first.run_id).steps["e"].skip_reason == BUDGET_SKIP_REASON

    harness.workflow("t", wf("  max_tokens: 10000", DIAMOND))
    harness.sink.clear()
    done = await harness.run(
        "t",
        providers={"claude": diamond_stub()},
        resume=first.run_id,
        options=RunOptions(force=True),
    )
    assert done.status is RunStatus.SUCCEEDED, done.reason
    assert sorted(done.reused) == ["a", "b", "c", "d"]
    assert harness.statuses(first.run_id)["e"] == "succeeded"
    assert done.usage.total == 1530


async def test_join_always_steps_still_run_after_the_cap_tripped(harness: Harness) -> None:
    """Review fix: drain semantics — nothing new starts except ``join: always`` steps."""
    harness.workflow(
        "t",
        wf(
            "  max_tokens: 500",
            """
  - {id: spend, prompt: "x"}
  - {id: more, needs: [spend], prompt: "never"}
  - {id: cleanup, needs: [spend], join: always, shell: "echo cleaned"}
  - {id: notify, needs: [more], join: always, shell: "echo notified"}
""",
        ),
    )
    result = await harness.run("t", providers={"claude": stub()})
    assert result.status is RunStatus.FAILED and "max_tokens 500" in (result.reason or "")
    assert harness.statuses(result.run_id) == {
        "spend": "succeeded",
        "more": "skipped",
        "cleanup": "succeeded",
        "notify": "succeeded",
    }
    run = harness.record(result.run_id)
    assert run.steps["more"].skip_reason == BUDGET_SKIP_REASON
    for sid, expected in (("cleanup", "cleaned"), ("notify", "notified")):
        text = harness.store.read_output(result.run_id, run.steps[sid].output_ref or "")
        assert text.strip().strip('"') == expected


# -- rayspec plan ------------------------------------------------------------------------------


def test_plan_shows_the_caps(tmp_path: Path) -> None:
    from rayspec.cli.app import app

    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "t.yaml").write_text(
        textwrap.dedent(
            """
            rayspec: 1
            name: t
            defaults: {budget_usd: "$1.50", max_tokens: 500k}
            steps:
              - {id: a, shell: "echo hi"}
            """
        )
    )
    env = {"RAYSPEC_HOME": str(tmp_path / "home"), "NO_COLOR": "1"}
    res = CliRunner().invoke(app, ["plan", "t", "--root", str(root)], env=env)
    assert res.exit_code == 0, res.output
    assert re.search(r"budget_usd \$1\.50", res.output) and "max_tokens 500,000" in res.output
    res = CliRunner().invoke(app, ["plan", "t", "--root", str(root), "--json"], env=env)
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["budget_usd"] == 1.5 and payload["max_tokens"] == 500_000
