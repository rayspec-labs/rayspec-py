# SPDX-License-Identifier: Apache-2.0
"""Stub ``expect:`` blocks: the stub asserts what the agent was actually asked.

A dry run proves the graph executed; ``expect:`` proves the ``AgentRequest`` carried the right
prompt, agent settings and session. A mismatch fails the step loudly with
``error.kind == "stub_expectation"`` and an excerpt of the rendered prompt.
"""

from __future__ import annotations

import pytest

from rayspec.providers.base import AgentError, AgentEvent, AgentRequest, AgentResult
from rayspec.providers.stub import (
    StubProvider,
    StubScript,
    StubScriptError,
    unmatched_expect_keys,
)

pytestmark = pytest.mark.anyio


class Collector:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


def _req(step_path: str = "review", prompt: str = "Fix the bug in parser.py", **kw) -> AgentRequest:
    return AgentRequest(step_path=step_path, prompt=prompt, cwd="/tmp", **kw)


async def _run(script: dict, req: AgentRequest) -> tuple[AgentResult, Collector]:
    provider = StubProvider(script=script)
    events = Collector()
    return await provider.run(req, events), events


def _error(result: AgentResult) -> AgentError:
    """The stub-expectation error of a failed result (a plain assert for the type checker)."""
    assert result.error is not None, result
    return result.error


# -- prompt expectations ----------------------------------------------------------------------


async def test_matching_prompt_contains_passes_through_to_the_scripted_answer():
    result, events = await _run(
        {"steps": {"review": {"text": "done", "expect": {"prompt_contains": "parser.py"}}}}, _req()
    )
    assert result.status == "success"
    assert result.text == "done"
    assert result.error is None
    assert "error" not in events.kinds()


async def test_mismatching_prompt_contains_fails_the_step_with_the_prompt_excerpt():
    result, events = await _run(
        {"steps": {"review": {"text": "done", "expect": {"prompt_contains": "lexer.py"}}}}, _req()
    )
    assert result.status == "error"
    assert result.error is not None
    assert _error(result).kind == "stub_expectation"
    assert result.error.transient is False
    assert "review" in _error(result).message
    assert "prompt_contains" in _error(result).message
    assert "lexer.py" in _error(result).message
    # the rendered prompt is shown so the author can see what the agent actually got
    assert "Fix the bug in parser.py" in _error(result).message
    assert result.text == ""
    assert events.kinds()[-1] == "error"


async def test_prompt_contains_accepts_a_list_and_reports_every_missing_needle():
    result, _ = await _run(
        {"steps": {"review": {"expect": {"prompt_contains": ["parser.py", "lexer.py", "ast.py"]}}}},
        _req(),
    )
    assert result.status == "error"
    assert "lexer.py" in _error(result).message and "ast.py" in _error(result).message


async def test_not_contains_hit_fails():
    result, _ = await _run({"steps": {"review": {"expect": {"not_contains": "parser.py"}}}}, _req())
    assert result.status == "error"
    assert "not_contains" in _error(result).message
    ok, _ = await _run({"steps": {"review": {"expect": {"not_contains": "TODO"}}}}, _req())
    assert ok.status == "success"


async def test_expect_prompt_regex_is_an_assertion_not_a_resolution_key():
    good, _ = await _run(
        {"steps": {"review": {"expect": {"prompt_regex": "bug in \\w+\\.py"}}}}, _req()
    )
    assert good.status == "success"
    bad, _ = await _run({"steps": {"review": {"expect": {"prompt_regex": "^Review"}}}}, _req())
    assert bad.status == "error" and "prompt_regex" in _error(bad).message


# -- request expectations ---------------------------------------------------------------------


async def test_model_and_access_expectations():
    script = {
        "steps": {"review": {"expect": {"model": "claude-sonnet-4-5", "access": "read-only"}}}
    }
    good, _ = await _run(script, _req(model="claude-sonnet-4-5", access="read-only"))
    assert good.status == "success"
    bad, _ = await _run(script, _req(model="gpt-5", access="read-only"))
    assert bad.status == "error"
    assert "model" in _error(bad).message and "gpt-5" in _error(bad).message


async def test_output_schema_expectation():
    script = {"steps": {"review": {"output": {"ok": True}, "expect": {"output_schema": True}}}}
    bad, _ = await _run(script, _req())
    assert bad.status == "error" and "output_schema" in _error(bad).message
    good, _ = await _run(script, _req(output_schema={"type": "object"}))
    assert good.status == "success"
    none_script = {"steps": {"review": {"expect": {"output_schema": False}}}}
    bad2, _ = await _run(none_script, _req(output_schema={"type": "object"}))
    assert bad2.status == "error"


async def test_session_expectation():
    resumed = {"steps": {"review": {"expect": {"session": "resumed"}}}}
    bad, _ = await _run(resumed, _req())
    assert bad.status == "error" and "session" in _error(bad).message
    good, _ = await _run(resumed, _req(resume_session="claude:abc"))
    assert good.status == "success"
    fresh = {"steps": {"review": {"expect": {"session": "fresh"}}}}
    assert (await _run(fresh, _req()))[0].status == "success"
    assert (await _run(fresh, _req(resume_session="claude:abc")))[0].status == "error"


# -- placement --------------------------------------------------------------------------------


async def test_expect_on_a_sequence_item_overrides_the_entry_expectation():
    script = {
        "steps": {
            "build[*]/implement": {
                "expect": {"prompt_contains": "task"},
                "sequence": [
                    {"text": "first"},
                    {"text": "second", "expect": {"prompt_contains": "retry"}},
                ],
            }
        }
    }
    provider = StubProvider(script=script)
    first = await provider.run(_req("build[1]/implement", "the task"), Collector())
    assert first.status == "success" and first.text == "first"
    second = await provider.run(_req("build[2]/implement", "the task"), Collector())
    assert second.status == "error" and "retry" in _error(second).message


async def test_expect_on_a_match_entry():
    script = {"match": [{"prompt_regex": "bug", "expect": {"prompt_contains": "lexer"}}]}
    result, _ = await _run(script, _req())
    assert result.status == "error" and _error(result).kind == "stub_expectation"


async def test_expectations_are_checked_before_a_scripted_failure():
    script = {"steps": {"review": {"fail": "boom", "expect": {"prompt_contains": "nope"}}}}
    result, _ = await _run(script, _req())
    assert _error(result).kind == "stub_expectation"


# -- parsing ----------------------------------------------------------------------------------


def test_unknown_expect_key_is_a_script_error():
    with pytest.raises(StubScriptError) as exc:
        StubScript.from_dict({"steps": {"a": {"expect": {"agent": "writer"}}}}, source="s.yaml")
    assert "steps.a.expect" in str(exc.value)
    assert "prompt_contains" in str(exc.value)


def test_bad_expect_values_are_script_errors():
    with pytest.raises(StubScriptError):
        StubScript.from_dict({"steps": {"a": {"expect": {"session": "maybe"}}}}, source="s")
    with pytest.raises(StubScriptError):
        StubScript.from_dict({"steps": {"a": {"expect": {"access": "root"}}}}, source="s")
    with pytest.raises(StubScriptError):
        StubScript.from_dict({"steps": {"a": {"expect": {"prompt_regex": "("}}}}, source="s")
    with pytest.raises(StubScriptError):
        StubScript.from_dict({"steps": {"a": {"expect": {"output_schema": "yes"}}}}, source="s")


def test_expect_round_trips_through_yaml():
    script = StubScript.from_yaml(
        "steps:\n  a:\n    expect:\n      prompt_contains: [x, y]\n      session: fresh\n",
        source="s.yaml",
    )
    expect = script.steps[0].outcome.expect
    assert expect is not None
    assert expect.prompt_contains == ("x", "y") and expect.session == "fresh"


# -- stale assertions -------------------------------------------------------------------------


def test_unmatched_expect_keys_finds_a_key_no_step_can_resolve():
    script = StubScript.from_dict(
        {
            "steps": {
                "ask": {"text": "a", "expect": {"prompt_contains": "x"}},
                "asq2": {"text": "b", "expect": {"prompt_contains": "y"}},
            }
        }
    )
    assert unmatched_expect_keys(script, ["ask", "ask2"]) == ("asq2",)


def test_unmatched_expect_keys_ignores_indices_and_globs():
    script = StubScript.from_dict(
        {
            "steps": {
                "build[*]/implement": {"expect": {"prompt_contains": "x"}},
                "fan[0]/patch": {"expect": {"prompt_contains": "y"}},
                "*": {"expect": {"prompt_contains": "z"}},
            }
        }
    )
    assert unmatched_expect_keys(script, ["build/implement", "fan/patch"]) == ()


def test_unmatched_expect_keys_ignores_entries_without_an_expect_block():
    script = StubScript.from_dict({"steps": {"gone": {"text": "stale"}}})
    assert unmatched_expect_keys(script, ["ask"]) == ()


def test_an_expect_block_on_a_sequence_item_counts_as_an_assertion():
    script = StubScript.from_dict(
        {"steps": {"gone": {"sequence": [{"text": "a"}, {"expect": {"model": "m"}}]}}}
    )
    assert unmatched_expect_keys(script, ["ask"]) == ("gone",)
