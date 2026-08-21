from __future__ import annotations

import pytest

from rayspec.schema import AgentDef, AgentOverride, InputSpec, SchemaError, inputs_to_json_schema
from rayspec.schema.agent import parse_agent_def


def test_agent_def_defaults():
    a = parse_agent_def({"provider": "claude"})
    assert a.provider == "claude" and a.model is None and a.effort is None
    assert a.access == "workspace-write" and a.instructions_mode == "append"
    assert a.tools.allow == [] and a.tools.deny == []
    assert a.max_turns is None and a.budget_usd is None and a.provider_options == {}


def test_agent_def_full():
    a = parse_agent_def(
        {
            "provider": "codex",
            "model": "medium",
            "effort": "high",
            "access": "read-only",
            "instructions_file": "prompts/x.md",
            "max_turns": 60,
            "budget_usd": 2.5,
            "tools": {"deny": ["web"], "allow": ["read", "mcp:github"]},
            "provider_options": {"codex": {"config": {"model_reasoning_effort": "high"}}},
        }
    )
    assert a.tools.deny == ["web"] and a.tools.allow == ["read", "mcp:github"]
    assert a.provider_options["codex"]["config"]["model_reasoning_effort"] == "high"
    assert a.access == "read-only" and a.budget_usd == 2.5


def test_agent_instructions_xor_file():
    with pytest.raises(SchemaError, match="instructions"):
        parse_agent_def({"instructions": "a", "instructions_file": "b.md"})


def test_agent_access_enum_and_unknown_field():
    with pytest.raises(SchemaError, match="access"):
        parse_agent_def({"access": "god-mode"})
    with pytest.raises(SchemaError, match="instructions_mode"):
        parse_agent_def({"instruction_mode": "append"})


def test_agent_override_requires_extends():
    o = AgentOverride.parse({"extends": "triage", "effort": "low"})
    assert o.extends == "triage" and o.effort == "low" and o.model is None
    assert AgentDef.parse({}).provider is None


def test_input_spec_defaults_and_json_schema():
    spec = InputSpec.parse({})
    assert spec.type == "string" and spec.required is False and spec.default is None
    schema = inputs_to_json_schema(
        {
            "issue": InputSpec.parse({"type": "integer", "required": True, "description": "n"}),
            "mode": InputSpec.parse({"enum": ["fast", "normal"], "default": "normal"}),
            "tags": InputSpec.parse({"type": "array", "items": {"type": "string"}, "default": []}),
        }
    )
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["required"] == ["issue"]
    assert schema["properties"]["issue"] == {"type": "integer", "description": "n"}
    assert schema["properties"]["mode"] == {
        "type": "string",
        "enum": ["fast", "normal"],
        "default": "normal",
    }
    assert schema["properties"]["tags"]["items"] == {"type": "string"}


def test_input_required_with_default_is_contradiction():
    with pytest.raises(SchemaError, match="default"):
        InputSpec.parse({"required": True, "default": "x"})


def test_input_type_must_be_json_schema_name():
    with pytest.raises(SchemaError, match="type"):
        InputSpec.parse({"type": "int"})
