"""Regression tests for the early contract review of the schema layer."""

from __future__ import annotations

import pytest

from rayspec.errors import RayspecError, UnsupportedFeatureError
from rayspec.providers.base import ProviderError
from rayspec.schema import (
    DEFAULT_PROMPT_RETRY,
    AgentDef,
    EachStep,
    LoopStep,
    PromptStep,
    SchemaError,
    StepModel,
    parse_step,
    parse_workflow,
)
from rayspec.schema.agent import McpServerDef, parse_agent_def


def test_as_item_is_writable_and_context_roots_still_reserved_for_as():
    step = parse_step({"id": "f", "each": "inputs.tags", "as": "item", "steps": []})
    assert isinstance(step, EachStep) and step.as_ == "item"
    with pytest.raises(SchemaError, match="reserved"):
        parse_step({"id": "f", "each": "inputs.tags", "as": "steps", "steps": []})


def test_dict_keys_may_use_context_root_names():
    wf = parse_workflow(
        {
            "rayspec": 1,
            "name": "x",
            "inputs": {"run": {}, "env": {}},
            "agents": {"env": {"provider": "claude"}},
            "steps": [{"id": "a", "shell": "true"}],
            "outputs": {"item": "{{ steps.a.output }}"},
        }
    )
    assert set(wf.inputs) == {"run", "env"} and "item" in wf.outputs
    with pytest.raises(SchemaError, match=r"inputs\.Bad-Name"):
        parse_workflow({"rayspec": 1, "name": "x", "inputs": {"Bad-Name": {}}, "steps": []})


def test_error_locations_do_not_include_discriminator_tag():
    with pytest.raises(SchemaError) as exc:
        parse_workflow(
            {
                "rayspec": 1,
                "name": "x",
                "steps": [
                    {"id": "ok", "shell": "true"},
                    {"id": "bad", "shell": "x", "join": "nope"},
                ],
            }
        )
    assert str(exc.value).splitlines()[0].startswith("steps[1] (id: bad).join:")
    with pytest.raises(SchemaError) as exc:
        parse_workflow(
            {
                "rayspec": 1,
                "name": "x",
                "steps": [
                    {
                        "id": "build",
                        "loop": {
                            "max_iterations": 1,
                            "steps": [
                                {"id": "a", "shell": "x"},
                                {"id": "c", "shell": "x", "join": "nope"},
                            ],
                        },
                    }
                ],
            }
        )
    assert "steps[0] (id: build).loop.steps[1] (id: c).join:" in str(exc.value)
    with pytest.raises(SchemaError) as exc:
        parse_step({"id": "a", "prompt": "x", "retry": {"attempts": 0}})
    assert str(exc.value).startswith("retry.attempts:")
    with pytest.raises(SchemaError) as exc:
        parse_step({"id": "Run-Tests", "shell": "true"})
    assert str(exc.value).startswith("id:")


def test_agent_thinking_and_mcp_fields():
    agent = parse_agent_def(
        {
            "provider": "claude",
            "thinking": True,
            "mcp": {
                "github": {"command": "gh-mcp", "args": ["--stdio"], "env": {"TOKEN": "x"}},
                "docs": {"transport": "http", "url": "https://example.com/mcp"},
            },
        }
    )
    assert agent.thinking is True
    assert isinstance(agent.mcp["github"], McpServerDef)
    assert agent.mcp["github"].transport == "stdio" and agent.mcp["docs"].transport == "http"
    with pytest.raises(SchemaError, match="url"):
        parse_agent_def({"mcp": {"x": {"transport": "http"}}})
    with pytest.raises(SchemaError, match="command"):
        parse_agent_def({"mcp": {"x": {}}})
    assert AgentDef.parse({}).thinking is None and AgentDef.parse({}).mcp == {}


def test_allow_failure_is_valid_on_any_step():
    loop = parse_step(
        {"id": "b", "loop": {"max_iterations": 1, "steps": []}, "allow_failure": True}
    )
    assert isinstance(loop, LoopStep) and loop.allow_failure is True
    approve = parse_step({"id": "g", "approve": "ok?", "allow_failure": True})
    assert approve.allow_failure is True


def test_retry_semantics_total_attempts_and_kind_default():
    assert DEFAULT_PROMPT_RETRY.attempts == 3 and DEFAULT_PROMPT_RETRY.delay == 3.0
    assert DEFAULT_PROMPT_RETRY.on_error == "transient"
    step = parse_step({"id": "a", "prompt": "x", "retry": {"attempts": 1}})
    assert isinstance(step, PromptStep) and step.retry is not None and step.retry.attempts == 1
    plain = parse_step({"id": "a", "prompt": "x"})
    assert isinstance(plain, PromptStep) and plain.retry is None  # None = kind default


def test_errors_share_a_root_and_unsupported_format():
    assert issubclass(SchemaError, RayspecError) and issubclass(ProviderError, RayspecError)
    err = UnsupportedFeatureError(
        path="agents.implementer.max_turns",
        value=60,
        provider="codex",
        capability="max_turns",
        capability_value=False,
        alternatives=["claude"],
        location=".rayspec/workflows/fix_issue.yaml:77",
    )
    text = str(err)
    assert text.splitlines() == [
        "unsupported: agents.implementer.max_turns = 60",
        "  provider 'codex' does not support `max_turns` (capability max_turns=False)",
        "  fix: remove it, use a provider that supports it (claude), or set defaults.on_unsupported: warn / --allow-unsupported",
        "  at .rayspec/workflows/fix_issue.yaml:77",
    ]
    assert err.hint and "on_unsupported" in err.hint


def test_multiple_unknown_keys_reported_together():
    with pytest.raises(SchemaError) as exc:
        parse_step({"id": "a", "prompt": "x", "allow_failur": True, "timeot": 3})
    msg = str(exc.value)
    assert "allow_failur" in msg and "timeot" in msg


def test_non_mapping_step_and_bad_agent_ref_messages():
    with pytest.raises(SchemaError, match="mapping"):
        parse_workflow({"rayspec": 1, "name": "x", "steps": ["foo"]})
    with pytest.raises(SchemaError, match="agent must be"):
        parse_step({"id": "a", "prompt": "x", "agent": 123})
    with pytest.raises(SchemaError, match="approve"):
        parse_step({"id": "a", "approve": 123})


def test_env_scalars_are_coerced_to_strings():
    step = parse_step(
        {"id": "a", "shell": "true", "env": {"N": 5, "FLAG": True, "F": 1.5, "S": "x"}}
    )
    assert step.env == {"N": "5", "FLAG": "true", "F": "1.5", "S": "x"}  # type: ignore[union-attr]


def test_step_model_alias_narrows_types():
    step: StepModel = parse_step({"id": "a", "shell": "true"})
    assert step.kind == "shell"


def test_schema_version_message():
    with pytest.raises(SchemaError, match="schema version"):
        parse_workflow({"rayspec": 2, "name": "x", "steps": []})


def test_timeout_must_be_positive():
    with pytest.raises(SchemaError, match="timeout"):
        parse_step({"id": "a", "shell": "true", "timeout": 0})
