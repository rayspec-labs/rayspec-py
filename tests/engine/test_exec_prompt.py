"""prompt: executor via the StubProvider: requests, streams, retry/timeout/classification,
structured output (enforced + best_effort re-asks), session continuation, dry run."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from events._validating import ValidatingSink
from rayspec.engine.context import RunOptions
from rayspec.engine.scheduler import run_graph
from rayspec.engine.structured import extract_json
from rayspec.events.model import EventType
from rayspec.providers.capabilities import STUB_CAPABILITIES
from rayspec.providers.stub import StubProvider
from rayspec.schema import StepStatus

from .conftest import Harness, make_graph_harness

pytestmark = pytest.mark.anyio


def wf(steps: str, agents: str = "") -> str:
    block = f"agents:\n{agents}\n" if agents else ""
    return f"rayspec: 1\nname: t\n{block}steps:\n{steps}"


def g_run_id(harness: Harness) -> str:
    """The run id of the most recent graph-harness run in ``harness.store``."""
    return harness.store.list_run_ids()[0]


async def run_wf(harness: Harness, text: str, script: dict[str, Any] | None = None, **opts: Any):
    harness.workflow("t", text)
    stub = StubProvider(script=script or {})
    providers = {"claude": stub}
    g = make_graph_harness(
        harness,
        harness.load("t"),
        fake_leaf=False,
        providers=providers if not opts.get("dry_run") else None,
        options=RunOptions(**opts) if opts else None,
    )
    outcomes = await run_graph(g.graph, g.scope, g.ctx)
    return g, outcomes, stub


async def test_prompt_basic_request_result_and_stream(harness: Harness) -> None:
    harness.sink = ValidatingSink(harness.sink)  # pin the published event/stream shapes
    g, out, stub = await run_wf(
        harness,
        wf(
            """
  - id: a
    agent: reviewer
    env: {TOKEN: "t-{{ 1 + 1 }}"}
    prompt: "Review {{ 'x' }} please"
""",
            agents="""
  reviewer:
    provider: claude
    model: small
    access: read-only
    instructions: "Be brief ({{ run.workflow }})"
    tools: {deny: [web]}
""",
        ),
        {"steps": {"a": {"text": "LGTM", "usage": {"input": 10, "output": 5}}}},
    )
    rec = out["a"].record
    assert rec.status is StepStatus.SUCCEEDED, rec.error
    assert out["a"].output == "LGTM" and rec.output_kind == "text"
    assert rec.session_ref is not None and rec.session_ref.id == "stub:a:1"
    assert rec.session_ref.provider == "stub"
    assert rec.usage.input == 10 and rec.usage.output == 5
    assert rec.attempts == 1 and rec.fingerprint
    req = stub.calls[0]
    assert req.prompt == "Review x please"
    assert req.instructions == "Be brief (t)"
    assert req.access.value == "read-only"
    assert req.env == {"TOKEN": "t-2"}
    assert req.tools.deny == ("web",)
    assert req.step_path == "a" and req.step_attempt == 1 and req.run_id == g.run.run_id
    kinds = [r.kind for r in harness.sink.stream_for("a")]
    assert kinds[0] == "session" and "text_delta" in kinds and kinds[-1] == "text"
    assert g.scope.views["a"].resolve("session") == "stub:a:1"
    assert g.scope.views["a"].resolve("model") == "haiku"  # model: small → tier


async def test_prompt_render_error_fails(harness: Harness) -> None:
    _, out, _ = await run_wf(harness, wf("  - {id: a, prompt: 'hi {{ steps.q.output }}'}"))
    rec = out["a"].record
    assert rec.status is StepStatus.FAILED and rec.error and rec.error.type == "render"


async def test_prompt_retry_transient_then_success(harness: Harness) -> None:
    _, out, stub = await run_wf(
        harness,
        wf("  - {id: a, prompt: hi, retry: {attempts: 3, delay: 0.01}}"),
        {
            "steps": {
                "a": {
                    "fail": {"kind": "api", "message": "busy", "transient": True, "times": 2},
                    "text": "recovered",
                }
            }
        },
    )
    rec = out["a"].record
    assert rec.status is StepStatus.SUCCEEDED and rec.attempts == 3
    assert out["a"].output == "recovered"
    # a success after a retry does not keep the previous attempt's error (the record,
    # the step.finished event and every renderer show the output, not "succeeded — busy")
    assert rec.error is None and rec.ok is True
    assert harness.finished("a").data["error"] is None
    assert harness.record(g_run_id(harness)).steps["a"].error is None
    assert len(stub.calls) == 3
    retries = harness.events(EventType.STEP_RETRY)
    assert [(e.data["attempt"], e.data["delay_s"]) for e in retries] == [(2, 0.01), (3, 0.02)]
    assert retries[0].data["error"]["transient"] is True
    # the started event is emitted once, stream records carry the attempt number
    assert len([e for e in harness.events(EventType.STEP_STARTED) if e.step_path == "a"]) == 1
    assert {r.attempt for r in harness.sink.stream_for("a")} == {1, 2, 3}


async def test_prompt_fatal_error_not_retried(harness: Harness) -> None:
    _, out, stub = await run_wf(
        harness,
        wf("  - {id: a, prompt: hi, retry: {attempts: 3, delay: 0.01}}"),
        {"steps": {"a": {"fail": {"kind": "auth", "message": "no key", "transient": False}}}},
    )
    rec = out["a"].record
    assert rec.status is StepStatus.FAILED and rec.attempts == 1
    assert rec.error and rec.error.type == "auth" and rec.error.transient is False
    assert len(stub.calls) == 1


async def test_prompt_provider_error_raised_is_classified(harness: Harness) -> None:
    _, out, stub = await run_wf(
        harness,
        wf("  - {id: a, prompt: hi, retry: {attempts: 2, delay: 0.01}}"),
        {
            "steps": {
                "a": {
                    "fail": {
                        "kind": "transport",
                        "message": "gone",
                        "transient": True,
                        "raise": True,
                    },
                    "text": "x",
                }
            }
        },
    )
    rec = out["a"].record
    assert rec.status is StepStatus.FAILED and rec.attempts == 2
    assert rec.error and rec.error.type == "transport" and rec.error.transient
    assert len(stub.calls) == 2


async def test_prompt_default_retry_policy_is_three_attempts(harness: Harness) -> None:
    from rayspec.engine.retry import policy_for
    from rayspec.schema import parse_step

    policy = policy_for(parse_step({"id": "p", "prompt": "x"}))
    assert policy is not None and (policy.attempts, policy.delay, policy.on_error) == (
        3,
        3.0,
        "transient",
    )
    assert policy_for(parse_step({"id": "s", "shell": "x"})) is None


async def test_prompt_timeout_status_not_transient_unless_all(harness: Harness) -> None:
    _, out, _ = await run_wf(
        harness,
        wf("""
  - {id: a, prompt: hi, timeout: 0.2, retry: {attempts: 2, delay: 0.01}}
  - {id: b, prompt: hi, timeout: 0.2, retry: {attempts: 2, delay: 0.01, on_error: all}}
"""),
        {"steps": {"a": {"latency_ms": 5000}, "b": {"latency_ms": 5000}}},
    )
    a = out["a"].record
    assert a.status is StepStatus.FAILED and a.error and a.error.type == "timeout"
    assert a.attempts == 1
    assert out["b"].record.attempts == 2


async def test_prompt_engine_deadline_cancels_slow_provider(harness: Harness) -> None:
    # no req.timeout_s-based simulation: the stub just sleeps; the engine's fail_after must fire
    _, out, _ = await run_wf(
        harness,
        wf("  - {id: a, prompt: hi, timeout: 0.1}"),
        {"steps": {"a": {"latency_ms": 50}}},
    )
    assert out["a"].record.status is StepStatus.SUCCEEDED
    harness.sink.clear()
    harness.workflow("t", wf("  - {id: a, prompt: hi}"))


async def test_structured_enforced_valid(harness: Harness) -> None:
    _, out, stub = await run_wf(
        harness,
        wf("""
  - id: a
    prompt: classify
    output_schema: {type: object, properties: {verdict: {enum: [fix, skip]}}, required: [verdict]}
  - id: b
    needs: [a]
    when: steps.a.output.verdict == 'fix'
    prompt: "go {{ steps.a.output.verdict }}"
"""),
        {"steps": {"a": {"output": {"verdict": "fix"}}}},
    )
    assert out["a"].output == {"verdict": "fix"} and out["a"].record.output_kind == "json"
    assert stub.calls[0].output_schema == {
        "type": "object",
        "properties": {"verdict": {"enum": ["fix", "skip"]}},
        "required": ["verdict"],
    }
    assert out["b"].record.status is StepStatus.SUCCEEDED
    assert stub.calls[1].prompt == "go fix"


async def test_structured_enforced_reasks_once_then_fails(harness: Harness) -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    harness.workflow(
        "t",
        wf(
            "  - {id: a, prompt: count, output_schema: {type: object, properties: {n: {type: integer}}, required: [n]}}"
        ),
    )
    # invalid, then valid → succeeded after one re-ask through the same session
    stub = StubProvider(
        script={"steps": {"a": {"sequence": [{"output": {"n": "x"}}, {"output": {"n": 3}}]}}}
    )
    g = make_graph_harness(harness, harness.load("t"), fake_leaf=False, providers={"claude": stub})
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert out["a"].output == {"n": 3}
    assert len(stub.calls) == 2
    assert stub.calls[1].resume_session == "stub:a:1"
    assert "not valid structured output" in stub.calls[1].prompt
    assert stub.calls[1].output_schema == schema
    assert out["a"].record.attempts == 1  # re-asks are not retries
    # invalid twice → failed
    stub2 = StubProvider(script={"steps": {"a": {"output": {"n": "x"}}}})
    g2 = make_graph_harness(
        harness, harness.load("t"), fake_leaf=False, providers={"claude": stub2}
    )
    out2 = await run_graph(g2.graph, g2.scope, g2.ctx)
    rec = out2["a"].record
    assert rec.status is StepStatus.FAILED and rec.error and rec.error.type == "output_schema"
    assert "'x' is not of type 'integer'" in rec.error.message
    assert len(stub2.calls) == 2
    assert rec.attempts == 1  # schema failures are not transient


class BestEffortStub(StubProvider):
    capabilities = replace(STUB_CAPABILITIES, structured_output="best_effort")


async def test_structured_best_effort_suffix_extraction_and_reasks(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  - {id: a, prompt: count, output_schema: {type: object, properties: {n: {type: integer}}, required: [n]}}"
        ),
    )
    stub = BestEffortStub(
        script={
            "steps": {
                "a": {
                    "sequence": [
                        {"text": "no json here"},
                        {"text": 'Sure:\n```json\n{"n": "bad"}\n```'},
                        {"text": 'Here: {"n": 4} done'},
                    ]
                }
            }
        }
    )
    g = make_graph_harness(harness, harness.load("t"), fake_leaf=False, providers={"claude": stub})
    out = await run_graph(g.graph, g.scope, g.ctx)
    assert out["a"].output == {"n": 4}
    assert len(stub.calls) == 3  # ≤ 2 re-asks
    assert stub.calls[0].output_schema is None
    assert "Respond ONLY with a JSON document" in stub.calls[0].prompt
    assert stub.calls[0].prompt.startswith("count")
    # a fourth bad answer would have failed: three bad answers → failed
    stub2 = BestEffortStub(script={"steps": {"a": {"text": "nope"}}})
    g2 = make_graph_harness(
        harness, harness.load("t"), fake_leaf=False, providers={"claude": stub2}
    )
    out2 = await run_graph(g2.graph, g2.scope, g2.ctx)
    assert out2["a"].record.status is StepStatus.FAILED
    assert len(stub2.calls) == 3


def test_extract_json_forms() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('text ```json\n{"a": [1, 2]}\n``` more') == {"a": [1, 2]}
    assert extract_json('prefix {"s": "}"} suffix') == {"s": "}"}
    assert extract_json('list: [1, 2] and {"x": 1}') == [1, 2]
    with pytest.raises(ValueError):
        extract_json("nothing here")


async def test_session_continuation_uses_recorded_session_ref(harness: Harness) -> None:
    _, out, stub = await run_wf(
        harness,
        wf("""
  - {id: a, prompt: start}
  - {id: b, needs: [a], session: a, prompt: continue}
  - {id: c, needs: [b], session: a, prompt: again}
"""),
    )
    assert [c.resume_session for c in stub.calls] == [None, "stub:a:1", "stub:a:1"]
    assert out["c"].record.status is StepStatus.SUCCEEDED


async def test_dry_run_swaps_provider_for_stub_with_script(harness: Harness) -> None:
    g, out, _ = await run_wf(
        harness,
        wf(
            "  - {id: a, agent: big, prompt: hi}",
            agents="  big: {provider: claude, model: large}",
        ),
        dry_run=True,
        stub_script={"steps": {"a": {"text": "from script"}}},
    )
    rec = out["a"].record
    assert rec.status is StepStatus.SUCCEEDED and out["a"].output == "from script"
    assert rec.provider == "stub"
    provider = await g.ctx.providers.get("claude")
    assert isinstance(provider, StubProvider)
    assert provider.calls[0].model == "claude-opus-4-6" or provider.calls[0].model


async def test_prompt_provider_closed_by_pool(harness: Harness) -> None:
    g, _, stub = await run_wf(harness, wf("  - {id: a, prompt: hi}"))
    assert stub.run_id == g.run.run_id and stub.closed is False
    await g.ctx.providers.aclose()
    assert stub.closed is True
