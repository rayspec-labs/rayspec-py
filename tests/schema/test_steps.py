from __future__ import annotations

import pytest

from rayspec.schema import (
    AgentDef,
    AgentOverride,
    ApproveStep,
    EachStep,
    IncludeStep,
    LoopStep,
    PromptStep,
    PythonStep,
    SchemaError,
    ShellStep,
    StopStep,
    parse_step,
)


def test_prompt_step_minimal_defaults():
    step = parse_step({"id": "assess", "prompt": "hi"})
    assert isinstance(step, PromptStep)
    assert step.kind == "prompt"
    assert step.prompt == "hi" and step.prompt_file is None and step.agent is None
    assert step.needs == [] and step.join == "all" and step.when is None
    assert step.timeout is None and step.always_run is False and step.allow_failure is False
    assert step.retry is None and step.output_schema is None and step.session is None
    assert step.env == {}


def test_prompt_file_also_discriminates_to_prompt_kind():
    step = parse_step({"id": "a", "prompt_file": "prompts/a.md"})
    assert isinstance(step, PromptStep) and step.prompt_file == "prompts/a.md"


def test_prompt_and_prompt_file_are_mutually_exclusive():
    with pytest.raises(SchemaError, match="prompt_file"):
        parse_step({"id": "a", "prompt": "x", "prompt_file": "y.md"})


def test_agent_reference_forms():
    assert parse_step({"id": "a", "prompt": "x", "agent": "triage"}).agent == "triage"  # type: ignore[union-attr]
    override = parse_step(
        {"id": "a", "prompt": "x", "agent": {"extends": "triage", "model": "large"}}
    )
    assert isinstance(override.agent, AgentOverride)  # type: ignore[union-attr]
    assert override.agent.extends == "triage" and override.agent.model == "large"  # type: ignore[union-attr]
    inline = parse_step(
        {"id": "a", "prompt": "x", "agent": {"provider": "codex", "model": "small"}}
    )
    assert isinstance(inline.agent, AgentDef) and inline.agent.provider == "codex"  # type: ignore[union-attr]


def test_shell_and_python_steps():
    sh = parse_step({"id": "t", "shell": "pytest -q", "allow_failure": True, "timeout": "5m"})
    assert isinstance(sh, ShellStep) and sh.kind == "shell"
    assert sh.interpreter == "bash" and sh.allow_failure is True and sh.timeout == 300.0
    py = parse_step({"id": "p", "python": "print(1)", "deps": ["httpx"], "cwd": "sub"})
    assert isinstance(py, PythonStep) and py.deps == ["httpx"] and py.cwd == "sub"


def test_retry_policy_parsing_and_defaults():
    step = parse_step({"id": "a", "prompt": "x", "retry": {"attempts": 3, "delay": "3s"}})
    assert isinstance(step, PromptStep) and step.retry is not None
    assert step.retry.attempts == 3 and step.retry.delay == 3.0
    assert step.retry.on_error == "transient"
    with pytest.raises(SchemaError, match="attempts"):
        parse_step({"id": "a", "prompt": "x", "retry": {"attempts": 0}})


def test_loop_step_requires_max_iterations_and_has_defaults():
    step = parse_step(
        {"id": "build", "loop": {"max_iterations": 3, "steps": [{"id": "x", "shell": "true"}]}}
    )
    assert isinstance(step, LoopStep) and step.kind == "loop"
    assert step.loop.max_iterations == 3 and step.loop.until is None
    assert step.loop.on_exhausted == "fail" and len(step.loop.steps) == 1
    with pytest.raises(SchemaError, match="max_iterations"):
        parse_step({"id": "build", "loop": {"steps": [{"id": "x", "shell": "true"}]}})


def test_each_step_defaults():
    step = parse_step(
        {"id": "fan", "each": "steps.find.output.items", "steps": [{"id": "one", "shell": "true"}]}
    )
    assert isinstance(step, EachStep) and step.kind == "each"
    assert step.each == "steps.find.output.items" and step.as_ == "item"
    assert step.max_parallel is None and step.on_failure == "fail"
    custom = parse_step(
        {
            "id": "fan",
            "each": "inputs.tags",
            "as": "tag",
            "max_parallel": 2,
            "on_failure": "continue",
            "steps": [{"id": "one", "shell": "true"}],
        }
    )
    assert custom.as_ == "tag" and custom.max_parallel == 2 and custom.on_failure == "continue"  # type: ignore[union-attr]


def test_approve_step_string_and_mapping_forms():
    short = parse_step({"id": "gate", "approve": "Ship it?"})
    assert isinstance(short, ApproveStep) and short.kind == "approve"
    assert short.approve.message == "Ship it?" and short.approve.on_reject == "cancel"
    long = parse_step({"id": "gate", "approve": {"message": "Ship?", "on_reject": "continue"}})
    assert long.approve.on_reject == "continue"  # type: ignore[union-attr]


def test_include_and_stop_steps():
    inc = parse_step({"id": "review", "include": "review_block", "with": {"target": "src/"}})
    assert isinstance(inc, IncludeStep) and inc.kind == "include"
    assert inc.include == "review_block" and inc.with_ == {"target": "src/"}
    stop = parse_step({"id": "bail", "stop": {"status": "cancelled", "reason": "nope"}})
    assert isinstance(stop, StopStep) and stop.kind == "stop"
    assert stop.stop.status == "cancelled" and stop.stop.reason == "nope"
    with pytest.raises(SchemaError, match="status"):
        parse_step({"id": "bail", "stop": {"status": "weird"}})


def test_missing_kind_key_lists_valid_kinds():
    with pytest.raises(SchemaError) as exc:
        parse_step({"id": "a", "needs": ["b"]})
    msg = str(exc.value)
    assert "kind key" in msg and "prompt" in msg and "shell" in msg and "stop" in msg


def test_multiple_kind_keys_rejected():
    with pytest.raises(SchemaError, match="multiple kind keys"):
        parse_step({"id": "a", "prompt": "x", "shell": "ls"})


def test_unknown_field_gets_did_you_mean():
    with pytest.raises(SchemaError) as exc:
        parse_step({"id": "a", "prompt": "x", "allow_failur": True})
    assert "allow_failure" in str(exc.value)


def test_field_not_valid_on_kind_is_rejected():
    with pytest.raises(SchemaError, match="retry"):
        parse_step(
            {"id": "b", "loop": {"max_iterations": 1, "steps": []}, "retry": {"attempts": 2}}
        )
    with pytest.raises(SchemaError, match="session"):
        parse_step({"id": "s", "shell": "ls", "session": "s"})


@pytest.mark.parametrize("bad", ["Run", "run-tests", "1x", "steps", "inputs", "each"])
def test_invalid_or_reserved_ids_rejected(bad):
    with pytest.raises(SchemaError, match="id"):
        parse_step({"id": bad, "shell": "true"})


def test_join_and_when_and_needs():
    step = parse_step(
        {"id": "a", "shell": "true", "needs": ["b", "c"], "join": "any", "when": "steps.b.ok"}
    )
    assert step.needs == ["b", "c"] and step.join == "any" and step.when == "steps.b.ok"
    with pytest.raises(SchemaError, match="join"):
        parse_step({"id": "a", "shell": "true", "join": "sometimes"})
