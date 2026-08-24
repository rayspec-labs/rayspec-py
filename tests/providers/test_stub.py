"""StubProvider: scripted outputs, resolution order, events, usage, sessions, timeouts."""

from __future__ import annotations

import json
import re

import anyio
import jsonschema
import pytest

from rayspec.errors import RayspecError
from rayspec.providers import stub as stub_mod
from rayspec.providers.base import (
    AgentEvent,
    AgentRequest,
    Provider,
    ProviderError,
    Usage,
)
from rayspec.providers.capabilities import STUB_CAPABILITIES
from rayspec.providers.registry import create_provider
from rayspec.providers.stub import StubProvider, StubScript, StubScriptError, minimal_instance

pytestmark = pytest.mark.anyio


class Collector:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


def _req(step_path: str = "review", prompt: str = "Review the code please", **kw) -> AgentRequest:
    return AgentRequest(step_path=step_path, prompt=prompt, cwd="/tmp", **kw)


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the durations the stub sleeps for, and return from each sleep at once.

    Only the stub module's view of ``anyio`` is replaced, so the event loop keeps the real one.
    Recording beats timing: the simulated latency is then asserted exactly, with no wall clock
    in the test at all.
    """
    slept: list[float] = []

    class _RecordingAnyio:
        def __getattr__(self, name: str):
            return getattr(anyio, name)

        async def sleep(self, seconds: float) -> None:
            slept.append(seconds)

    monkeypatch.setattr(stub_mod, "anyio", _RecordingAnyio())
    return slept


# -- protocol / lifecycle ---------------------------------------------------------------------


async def test_stub_implements_provider_protocol_and_lifecycle():
    provider = StubProvider({})
    assert isinstance(provider, Provider)
    assert provider.id == "stub"
    assert provider.capabilities is STUB_CAPABILITIES
    await provider.open(run_id="r1", workdir="/tmp", env={"A": "1"}, max_parallel=4)
    assert provider.run_id == "r1" and provider.max_parallel == 4
    health = await provider.healthcheck()
    assert health.ok and health.auth == "ok"
    health2 = await provider.healthcheck(probe=True)
    assert health2.ok
    await provider.aclose()


def test_registry_creates_stub_with_script_from_settings():
    provider = create_provider("stub", {"script": {"steps": {"review": {"text": "hi"}}}})
    assert isinstance(provider, StubProvider)
    assert provider.script.resolve("review", "x") is not None


# -- defaults ---------------------------------------------------------------------------------


async def test_default_output_events_usage_and_session_ref():
    provider = StubProvider()
    emit = Collector()
    prompt = "word " * 30  # > 80 chars
    result = await provider.run(_req("review", prompt), emit)
    assert result.status == "success"
    assert result.text == "[stub] " + prompt[:80]
    assert result.structured is None
    assert result.session_ref == "stub:review:1"
    assert result.usage == Usage(input=len(prompt) // 4, output=len(result.text) // 4)
    assert result.cost_usd is None and result.cost_source == "none"
    assert result.num_turns == 1 and result.model == "stub"
    kinds = emit.kinds()
    assert kinds[0] == "session"
    assert kinds[-1] == "text"
    deltas = [e for e in emit.events if e.kind == "text_delta"]
    assert len(deltas) > 1
    assert "".join(d.text for d in deltas) == result.text
    assert emit.events[-1].text == result.text
    # second call on the same path bumps the counter; other paths start at 1
    result2 = await provider.run(_req("review", prompt), Collector())
    assert result2.session_ref == "stub:review:2"
    result3 = await provider.run(_req("other", prompt), Collector())
    assert result3.session_ref == "stub:other:1"
    assert [c.step_path for c in provider.calls] == ["review", "review", "other"]
    assert provider.calls[0].prompt == prompt


async def test_default_output_with_schema_is_a_minimal_instance():
    schema = {
        "type": "object",
        "required": ["ok", "count", "kind", "items", "nested", "ratio", "tag"],
        "properties": {
            "ok": {"type": "boolean"},
            "count": {"type": "integer"},
            "kind": {"type": "string", "enum": ["bug", "feature"]},
            "items": {"type": "array", "items": {"type": "string"}},
            "nested": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}, "opt": {"type": "integer"}},
            },
            "ratio": {"type": "number"},
            "tag": {"type": "string", "default": "x"},
            "not_required": {"type": "string"},
        },
    }
    result = await provider_run(StubProvider(), _req(output_schema=schema))
    assert result.structured == {
        "ok": False,
        "count": 0,
        "kind": "bug",
        "items": [],
        "nested": {"name": ""},
        "ratio": 0.0,
        "tag": "x",
    }
    assert json.loads(result.text) == result.structured
    # non-object schemas still yield a typed default
    r2 = await provider_run(StubProvider(), _req(output_schema={"type": "array"}))
    assert r2.structured == []
    r3 = await provider_run(StubProvider(), _req(output_schema={"const": 7}))
    assert r3.structured == 7
    r4 = await provider_run(StubProvider(), _req(output_schema={"type": ["null", "string"]}))
    assert r4.structured is None


async def provider_run(provider: StubProvider, req: AgentRequest):
    return await provider.run(req, Collector())


# -- resolution order -------------------------------------------------------------------------

SCRIPT = {
    "steps": {
        "review": {"text": "exact"},
        "build[*]/implement": {"text": "glob"},
        "build[2]/implement": {"text": "exact-iter"},
    },
    "match": [
        {"prompt_regex": "fix (the )?bug", "text": "matched"},
        {"prompt_regex": "^never", "text": "never"},
    ],
    "defaults": {"latency_ms": 0},
}


async def test_resolution_order_exact_glob_match_default():
    provider = StubProvider(script=StubScript.from_dict(SCRIPT))
    assert (await provider_run(provider, _req("review", "fix the bug"))).text == "exact"
    assert (await provider_run(provider, _req("build[1]/implement", "fix bug"))).text == "glob"
    assert (await provider_run(provider, _req("build[2]/implement", "x"))).text == "exact-iter"
    assert (await provider_run(provider, _req("other", "please fix the bug"))).text == "matched"
    r = await provider_run(provider, _req("other", "nothing"))
    assert r.text == "[stub] nothing"
    assert r.raw.get("matched") is None
    assert (await provider_run(provider, _req("review", "x"))).raw["matched"] == "review"


def _key(script: StubScript, step_path: str, prompt: str) -> str:
    entry = script.resolve(step_path, prompt)
    assert entry is not None
    return entry.key


def test_script_resolve_returns_entries():
    script = StubScript.from_dict(SCRIPT)
    assert script.resolve("review", "") is not None
    assert _key(script, "build[7]/implement", "") == "build[*]/implement"
    assert _key(script, "zzz", "fix bug") == "fix (the )?bug"
    assert script.resolve("zzz", "") is None


# -- scripted outcomes ------------------------------------------------------------------------


async def test_output_dict_becomes_structured_plus_json_text():
    provider = StubProvider(script={"steps": {"assess": {"output": {"severity": "high", "n": 2}}}})
    result = await provider_run(provider, _req("assess"))
    assert result.structured == {"severity": "high", "n": 2}
    assert json.loads(result.text) == {"severity": "high", "n": 2}


async def test_sequence_returns_nth_then_repeats_last():
    provider = StubProvider(
        script={"steps": {"s": {"sequence": ["one", {"text": "two"}, {"output": {"k": 3}}]}}}
    )
    texts = []
    for _ in range(5):
        r = await provider_run(provider, _req("s"))
        texts.append(r.structured if r.structured is not None else r.text)
    assert texts == ["one", "two", {"k": 3}, {"k": 3}, {"k": 3}]
    assert (await provider_run(provider, _req("s"))).session_ref == "stub:s:6"


async def test_fail_times_then_success_and_error_event():
    provider = StubProvider(
        script={
            "steps": {
                "flaky": {
                    "fail": {"kind": "api", "message": "boom", "transient": True, "times": 2},
                    "text": "recovered",
                }
            }
        }
    )
    emit = Collector()
    r1 = await provider.run(_req("flaky"), emit)
    assert r1.status == "error" and r1.error is not None
    assert r1.error.kind == "api" and r1.error.message == "boom" and r1.error.transient is True
    assert r1.text == ""
    assert emit.kinds()[-1] == "error"
    r2 = await provider_run(provider, _req("flaky"))
    assert r2.status == "error"
    r3 = await provider_run(provider, _req("flaky"))
    assert r3.status == "success" and r3.text == "recovered"


async def test_fail_without_times_always_fails_and_raise_option_raises_provider_error():
    provider = StubProvider(script={"steps": {"dead": {"fail": {"message": "gone"}}}})
    for _ in range(3):
        assert (await provider_run(provider, _req("dead"))).status == "error"
    raiser = StubProvider(
        script={"steps": {"dead": {"fail": {"message": "gone", "raise": True, "transient": True}}}}
    )
    with pytest.raises(ProviderError) as info:
        await provider_run(raiser, _req("dead"))
    assert info.value.transient is True and "gone" in str(info.value)


async def test_scripted_events_are_emitted_before_the_answer_text():
    """Tool calls/results come first, then the answer (deltas + final text), so
    ``logs --step`` reads tool call → tool result → answer."""
    provider = StubProvider(
        script={
            "steps": {
                "impl": {
                    "text": "done",
                    "events": [
                        {"tool_call": {"name": "Bash", "call_id": "c1", "input": {"cmd": "ls"}}},
                        {"tool_result": {"call_id": "c1", "text": "a.py"}},
                        {"text": "intermediate"},
                        {"reasoning": "thinking..."},
                        {"kind": "warning", "text": "careful", "data": {"x": 1}},
                    ],
                }
            }
        }
    )
    emit = Collector()
    await provider.run(_req("impl"), emit)
    kinds = emit.kinds()
    assert kinds[0] == "session"
    assert kinds.index("tool_call") < kinds.index("text_delta")  # events first, then the answer
    assert kinds[-1] == "text" and emit.events[-1].text == "done"
    # the answer is streamed as deltas immediately followed by the final text record
    first_delta = kinds.index("text_delta")
    assert set(kinds[first_delta:-1]) == {"text_delta"}
    idx = kinds.index("tool_call")
    assert kinds[idx : idx + 5] == ["tool_call", "tool_result", "text", "reasoning", "warning"]
    call = emit.events[idx]
    assert call.name == "Bash" and call.call_id == "c1" and call.data == {"input": {"cmd": "ls"}}
    res = emit.events[idx + 1]
    assert res.call_id == "c1" and res.text == "a.py"
    assert emit.events[idx + 2].text == "intermediate"
    assert emit.events[idx + 3].text == "thinking..."
    assert emit.events[idx + 4].data == {"x": 1}


async def test_usage_scripted_per_step_or_from_defaults():
    provider = StubProvider(
        script={
            "steps": {"a": {"text": "x", "usage": {"input": 10, "output": 5, "cached_input": 2}}},
            "defaults": {"usage": {"input": 100, "output": 50}},
        }
    )
    assert (await provider_run(provider, _req("a"))).usage == Usage(
        input=10, output=5, cached_input=2
    )
    assert (await provider_run(provider, _req("b"))).usage == Usage(input=100, output=50)


async def test_latency_and_timeout_simulation(monkeypatch: pytest.MonkeyPatch):
    provider = StubProvider(
        script={"steps": {"slow": {"text": "ok", "latency_ms": 5}}, "defaults": {"latency_ms": 1}}
    )
    slept = _record_sleeps(monkeypatch)
    r = await provider_run(provider, _req("slow"))
    assert r.status == "success"
    assert slept == [0.005]  # the scripted latency is actually awaited, not just parsed
    slept.clear()
    r2 = await provider_run(provider, _req("slow", timeout_s=0.001))
    assert r2.status == "timeout"
    assert r2.error is not None and r2.error.kind == "timeout"
    # default latency applies to unscripted steps too; a generous timeout passes
    slept.clear()
    r3 = await provider_run(provider, _req("fast", timeout_s=5))
    assert r3.status == "success"
    assert slept == [0.001]  # defaults.latency_ms, not the 5 ms scripted on "slow"


async def test_scripted_status_override_and_model_passthrough():
    provider = StubProvider(script={"steps": {"s": {"text": "partial", "status": "max_turns"}}})
    r = await provider_run(provider, _req("s", model="gpt-x"))
    assert r.status == "max_turns" and r.text == "partial" and r.model == "gpt-x"


async def test_explicit_sessions_are_honoured_in_raw_and_resume_keeps_counter():
    provider = StubProvider()
    r1 = await provider_run(provider, _req("s"))
    r2 = await provider_run(provider, _req("s", resume_session=r1.session_ref, fork_session=True))
    assert r2.session_ref == "stub:s:2"
    assert r2.raw["resume_session"] == "stub:s:1" and r2.raw["fork_session"] is True


# -- script parsing ---------------------------------------------------------------------------


def test_from_yaml_and_defaults():
    script = StubScript.from_yaml(
        """
steps:
  review:
    text: hi
defaults:
  latency_ms: 3
  usage: {input: 1, output: 2}
""",
        source="stubs.yaml",
    )
    assert script.defaults.latency_ms == 3
    assert script.defaults.usage == Usage(input=1, output=2)
    entry = script.resolve("review", "")
    assert entry is not None and entry.outcome_for(1).text == "hi"


def test_from_file(tmp_path):
    path = tmp_path / "stubs.yaml"
    path.write_text("steps:\n  a: {text: z}\n")
    script = StubScript.from_file(path)
    assert script.resolve("a", "") is not None


@pytest.mark.parametrize(
    "data",
    [
        {"steps": {"a": {"bogus": 1}}},
        {"steps": {"a": "not a mapping"}},
        {"steps": {"a": {"sequence": "not a list"}}},
        {"steps": {"a": {"fail": {"kind": "nope"}}}},
        {"steps": {"a": {"events": [{"unknown_kind": 1}]}}},
        {"steps": {"a": {"usage": {"tokens": 1}}}},
        {"steps": {"a": {"status": "bogus"}}},
        {"match": [{"text": "no regex"}]},
        {"match": [{"prompt_regex": "(", "text": "bad regex"}]},
        {"matches": []},
        {"defaults": {"latency_ms": "slow"}},
        ["not", "a", "mapping"],
    ],
)
def test_from_dict_rejects_malformed_scripts(data):
    with pytest.raises(StubScriptError) as info:
        StubScript.from_dict(data, source="t")
    assert isinstance(info.value, RayspecError)
    assert "t" in str(info.value)


def test_from_dict_accepts_none_and_empty():
    assert StubScript.from_dict(None).resolve("x", "") is None
    assert StubScript.from_dict({}).steps == ()


def test_match_regex_is_compiled_with_search_semantics():
    script = StubScript.from_dict({"match": [{"prompt_regex": "b.g", "text": "t"}]})
    entry = script.resolve("any", "a big one")
    assert entry is not None and isinstance(entry.prompt_regex, re.Pattern)


def test_registry_creates_stub_from_script_path(tmp_path):
    path = tmp_path / "stubs.yaml"
    path.write_text("steps:\n  a: {text: from-file}\n")
    provider = create_provider("stub", {"script_path": str(path)})
    assert isinstance(provider, StubProvider)
    entry = provider.script.resolve("a", "")
    assert entry is not None and entry.outcome_for(1).text == "from-file"


# -- review fixes: counters ------------------------------------------------------------------


async def test_glob_entry_sequence_advances_across_matched_paths():
    """A ``sequence`` on a glob entry counts calls per *entry*, so loop iterations converge."""
    provider = StubProvider(
        script={"steps": {"build[*]/implement": {"sequence": ["not yet", "not yet", "done"]}}}
    )
    texts = [
        (await provider_run(provider, _req(f"build[{i}]/implement"))).text for i in (1, 2, 3, 4)
    ]
    assert texts == ["not yet", "not yet", "done", "done"]
    # session_ref and fail.times stay per path
    r = await provider_run(provider, _req("build[1]/implement"))
    assert r.session_ref == "stub:build[1]/implement:2"


async def test_fail_times_is_counted_per_path_under_a_glob():
    provider = StubProvider(
        script={
            "steps": {
                "fix[*]/try": {"fail": {"kind": "api", "times": 1, "transient": True}, "text": "ok"}
            }
        }
    )
    assert (await provider_run(provider, _req("fix[1]/try"))).status == "error"
    assert (await provider_run(provider, _req("fix[1]/try"))).status == "success"
    assert (await provider_run(provider, _req("fix[2]/try"))).status == "error"
    assert (await provider_run(provider, _req("fix[2]/try"))).status == "success"


# -- review fixes: script values must be JSON -------------------------------------------------


def test_output_and_event_data_must_be_json_serialisable():
    with pytest.raises(StubScriptError, match=r"steps\.a\.output: not JSON-serialisable"):
        StubScript.from_yaml("steps:\n  a: {output: {when: 2024-01-01}}\n", source="t")
    with pytest.raises(StubScriptError, match=r"sequence\[0\]\.output"):
        StubScript.from_yaml("steps:\n  a:\n    sequence: [{output: {when: 2024-01-01}}]\n")
    with pytest.raises(StubScriptError, match=r"events\[0\]\.data"):
        StubScript.from_yaml(
            "steps:\n  a:\n    events: [{tool_call: {name: x, input: {d: 2024-01-01}}}]\n"
        )
    # plain JSON values are fine
    script = StubScript.from_yaml("steps:\n  a: {output: {n: 1, s: x, l: [1, 2], z: null}}\n")
    entry = script.resolve("a", "")
    assert entry is not None and entry.outcome.output == {"n": 1, "s": "x", "l": [1, 2], "z": None}


# -- review fixes: minimal_instance refs and constraints -------------------------------------


def test_minimal_instance_resolves_local_refs_and_min_constraints():
    schema = {
        "type": "object",
        "required": ["a", "b", "items", "n", "f", "deep", "rec"],
        "properties": {
            "a": {"$ref": "#/$defs/S"},
            "b": {"$ref": "#/definitions/Legacy"},
            "items": {"type": "array", "minItems": 2, "items": {"$ref": "#/$defs/S"}},
            "n": {"type": "integer", "minimum": 3},
            "f": {"type": "number", "exclusiveMinimum": 0},
            "deep": {"$ref": "#/$defs/Deep"},
            "rec": {"$ref": "#/$defs/Node"},
        },
        "$defs": {
            "S": {"type": "string", "minLength": 2},
            "Deep": {
                "type": "object",
                "required": ["inner"],
                "properties": {"inner": {"$ref": "#/$defs/S"}},
            },
            "Node": {
                "type": "object",
                "required": ["child"],
                "properties": {"child": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/Node"}]}},
            },
        },
        "definitions": {"Legacy": {"type": "integer", "maximum": -2}},
    }
    value = minimal_instance(schema)
    jsonschema.validate(value, schema)
    assert value["a"] == "xx" and value["b"] == -2
    assert value["items"] == ["xx", "xx"] and value["n"] == 3 and value["f"] > 0
    assert value["deep"] == {"inner": "xx"} and value["rec"] == {"child": None}
    # a directly self-referential required ref terminates (recursion guard)
    loop = {
        "$ref": "#/$defs/L",
        "$defs": {
            "L": {
                "type": "object",
                "required": ["next"],
                "properties": {"next": {"$ref": "#/$defs/L"}},
            }
        },
    }
    assert minimal_instance(loop) == {"next": {}}
    # unknown ref → {} (documented limitation), never raises
    assert minimal_instance({"$ref": "#/$defs/missing"}) == {}
    assert minimal_instance({"$ref": "https://example.com/x.json"}) == {}


# -- review fixes: healthcheck and timeout simulation ---------------------------------------


async def test_healthcheck_reports_no_sdk_version():
    health = await StubProvider().healthcheck()
    assert health.ok and health.sdk_version is None
    assert any("rayspec" in d for d in health.details)


async def test_timeout_simulation_returns_before_the_engine_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    """The scripted ``timeout`` result must be observable under ``anyio.fail_after(timeout_s)``."""
    provider = StubProvider(script={"steps": {"slow": {"text": "ok", "latency_ms": 5000}}})
    slept = _record_sleeps(monkeypatch)
    r = await provider_run(provider, _req("slow", timeout_s=0.2))
    assert r.status == "timeout" and r.error is not None and r.error.kind == "timeout"
    # "before the deadline" is the contract: the simulated provider-side delay must be strictly
    # shorter than the engine's own fail_after(timeout_s), or the timeout status is unobservable.
    assert slept == [0.1]
    assert slept[0] < 0.2
