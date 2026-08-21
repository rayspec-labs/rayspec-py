from __future__ import annotations

import pytest

from rayspec.schema import (
    LoopStep,
    PromptStep,
    SchemaError,
    ShellStep,
    StopStep,
    Workflow,
    parse_workflow,
)

FULL = {
    "rayspec": 1,
    "name": "fix_issue",
    "description": "Fix an issue.",
    "inputs": {
        "issue": {"type": "integer", "required": True},
        "base": {"default": "main"},
    },
    "defaults": {"agent": "implementer", "timeout": "30m", "max_parallel": 4},
    "agents": {
        "triage": {"provider": "claude", "model": "small", "access": "read-only"},
        "implementer": {"provider": "codex", "model": "medium", "max_turns": 60},
    },
    "steps": [
        {"id": "fetch", "shell": "gh issue view 1"},
        {
            "id": "assess",
            "needs": ["fetch"],
            "agent": "triage",
            "prompt": "{{ steps.fetch.output }}",
            "output_schema": {
                "type": "object",
                "properties": {"verdict": {"enum": ["fix", "skip"]}},
            },
        },
        {
            "id": "bail",
            "needs": ["assess"],
            "when": "steps.assess.output.verdict == 'skip'",
            "stop": {"status": "cancelled", "reason": "skip"},
        },
        {
            "id": "build",
            "needs": ["assess"],
            "loop": {
                "max_iterations": 3,
                "until": "steps.review.output | has_signal('DONE')",
                "steps": [
                    {"id": "implement", "prompt": "fix it", "session": "implement"},
                    {
                        "id": "check",
                        "needs": ["implement"],
                        "shell": "pytest",
                        "allow_failure": True,
                    },
                    {"id": "review", "needs": ["check"], "agent": "triage", "prompt": "review"},
                ],
            },
        },
        {"id": "confirm", "needs": ["build"], "approve": "Open a PR?"},
    ],
    "outputs": {"verdict": "{{ steps.assess.output.verdict }}"},
}


def test_parse_full_workflow():
    wf = parse_workflow(FULL)
    assert isinstance(wf, Workflow)
    assert wf.rayspec == 1 and wf.name == "fix_issue"
    assert wf.isolation == "worktree"
    assert wf.defaults.agent == "implementer" and wf.defaults.timeout == 1800.0
    assert wf.defaults.max_parallel == 4 and wf.defaults.on_unsupported == "error"
    assert wf.defaults.on_step_failure == "drain"
    assert set(wf.agents) == {"triage", "implementer"}
    assert wf.agents["implementer"].max_turns == 60
    kinds = [type(s) for s in wf.steps]
    assert kinds == [ShellStep, PromptStep, StopStep, LoopStep, type(wf.steps[4])]
    assert wf.outputs == {"verdict": "{{ steps.assess.output.verdict }}"}
    assert wf.inputs["issue"].required is True and wf.inputs["base"].default == "main"


def test_workflow_minimal_defaults():
    wf = parse_workflow({"rayspec": 1, "name": "x", "steps": [{"id": "a", "shell": "true"}]})
    assert wf.description == "" and wf.inputs == {} and wf.agents == {} and wf.outputs == {}
    assert wf.defaults.on_unsupported == "error" and wf.defaults.max_parallel == 4


def test_workflow_requires_schema_version_and_name():
    with pytest.raises(SchemaError, match="rayspec"):
        parse_workflow({"name": "x", "steps": []})
    with pytest.raises(SchemaError, match="rayspec"):
        parse_workflow({"rayspec": 2, "name": "x", "steps": []})
    with pytest.raises(SchemaError, match="name"):
        parse_workflow({"rayspec": 1, "steps": []})


def test_workflow_name_must_be_identifier():
    with pytest.raises(SchemaError, match="name"):
        parse_workflow({"rayspec": 1, "name": "Fix Issue", "steps": []})


def test_duplicate_step_ids_anywhere_in_file_are_rejected():
    data = {
        "rayspec": 1,
        "name": "x",
        "steps": [
            {"id": "a", "shell": "true"},
            {"id": "b", "loop": {"max_iterations": 1, "steps": [{"id": "a", "shell": "true"}]}},
        ],
    }
    with pytest.raises(SchemaError, match="duplicate"):
        parse_workflow(data)


def test_unknown_top_level_key_with_did_you_mean():
    with pytest.raises(SchemaError) as exc:
        parse_workflow({"rayspec": 1, "name": "x", "steps": [], "defualts": {}})
    assert "defaults" in str(exc.value)


def test_error_locations_are_id_aware():
    data = {
        "rayspec": 1,
        "name": "x",
        "steps": [{"id": "ok", "shell": "true"}, {"id": "bad", "shell": "x", "join": "nope"}],
    }
    with pytest.raises(SchemaError) as exc:
        parse_workflow(data)
    assert "steps[1]" in str(exc.value) and "bad" in str(exc.value)


def test_schema_error_exposes_messages():
    with pytest.raises(SchemaError) as exc:
        parse_workflow({"rayspec": 1, "name": "x", "steps": [{"id": "a"}]})
    assert isinstance(exc.value.errors, list) and exc.value.errors
