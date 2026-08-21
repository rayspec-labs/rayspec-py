"""resolve_inputs: precedence, coercion, errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.errors import InputError
from rayspec.loader.inputs import resolve_inputs
from rayspec.schema import parse_workflow

WF = parse_workflow(
    {
        "rayspec": 1,
        "name": "wf",
        "inputs": {
            "issue": {"type": "integer", "required": True},
            "base": {"type": "string", "default": "main"},
            "mode": {"type": "string", "enum": ["fast", "normal"], "default": "normal"},
            "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            "nums": {"type": "array", "items": {"type": "integer"}},
            "ratio": {"type": "number"},
            "dry": {"type": "boolean", "default": False},
            "meta": {"type": "object"},
            "name": {"type": "string"},
        },
        "steps": [{"id": "a", "shell": "echo"}],
    }
)


def test_precedence_cli_over_file_over_env_over_default(tmp_path: Path):
    f = tmp_path / "in.yaml"
    f.write_text("issue: 2\nbase: file\nmode: fast\n")
    env = {
        "RAYSPEC_INPUT_ISSUE": "3",
        "RAYSPEC_INPUT_BASE": "env",
        "RAYSPEC_INPUT_MODE": "normal",
        "RAYSPEC_INPUT_NAME": "from-env",
    }
    out = resolve_inputs(WF, cli_pairs=["issue=1"], inputs_file=f, env=env)
    assert out["issue"] == 1
    assert out["base"] == "file"
    assert out["mode"] == "fast"
    assert out["name"] == "from-env"
    assert out["tags"] == []
    assert out["dry"] is False
    assert "ratio" not in out and "meta" not in out


def test_coercion_by_type():
    out = resolve_inputs(
        WF,
        cli_pairs=[
            "issue=7",
            "ratio=0.5",
            "dry=yes",
            "tags=a",
            "tags=b",
            "nums=1",
            "nums=2",
            'meta={"k": [1]}',
        ],
        env={},
    )
    assert out["issue"] == 7
    assert out["ratio"] == 0.5
    assert out["dry"] is True
    assert out["tags"] == ["a", "b"]
    assert out["nums"] == [1, 2]
    assert out["meta"] == {"k": [1]}
    out = resolve_inputs(WF, cli_pairs=["issue=7", 'tags=["x","y"]', "dry=0"], env={})
    assert out["tags"] == ["x", "y"]
    assert out["dry"] is False


def test_json_inputs_file(tmp_path: Path):
    f = tmp_path / "in.json"
    f.write_text('{"issue": 4, "tags": ["t"], "dry": true}')
    out = resolve_inputs(WF, inputs_file=f, env={})
    assert out["issue"] == 4 and out["tags"] == ["t"] and out["dry"] is True


def test_errors_are_collected(tmp_path: Path):
    with pytest.raises(InputError) as ei:
        resolve_inputs(WF, cli_pairs=["isue=1", "ratio=abc", "dry=maybe", "bogus"], env={})
    msg = str(ei.value)
    assert "unknown input 'isue'; did you mean 'issue'?" in msg
    assert "input 'ratio': expected a number, got 'abc'" in msg
    assert "input 'dry': expected a boolean" in msg
    assert "invalid --input 'bogus': expected NAME=VALUE" in msg
    assert "missing required input(s): issue" in msg
    assert "RAYSPEC_INPUT_ISSUE" in msg


def test_missing_required_reported_together():
    wf = parse_workflow(
        {
            "rayspec": 1,
            "name": "wf",
            "inputs": {"a": {"required": True}, "b": {"required": True}},
            "steps": [{"id": "s", "shell": "echo"}],
        }
    )
    with pytest.raises(InputError, match=r"missing required input\(s\): a, b"):
        resolve_inputs(wf, env={})


def test_schema_validation_enum_and_types():
    with pytest.raises(InputError, match="input 'mode'"):
        resolve_inputs(WF, cli_pairs=["issue=1", "mode=slow"], env={})
    with pytest.raises(InputError, match=r"input 'nums\.0'"):
        resolve_inputs(WF, cli_pairs=["issue=1", 'nums=["a"]'], env={})


def test_env_coercion_error_names_variable():
    with pytest.raises(InputError, match="RAYSPEC_INPUT_ISSUE"):
        resolve_inputs(WF, env={"RAYSPEC_INPUT_ISSUE": "x"})


def test_no_inputs_declared():
    wf = parse_workflow({"rayspec": 1, "name": "wf", "steps": [{"id": "s", "shell": "echo"}]})
    assert resolve_inputs(wf, env={}) == {}
    with pytest.raises(InputError, match="declares no inputs"):
        resolve_inputs(wf, cli_pairs=["x=1"], env={})


def test_repeated_scalar_input_is_an_error():
    with pytest.raises(InputError) as exc:
        resolve_inputs(WF, cli_pairs=["issue=1", "issue=2"])
    assert exc.value.errors == [
        "--input: input 'issue' given more than once (only array inputs may be repeated)"
    ]
    # arrays may repeat; the error is reported once per duplicated scalar
    values = resolve_inputs(WF, cli_pairs=["issue=1", "tags=a", "tags=b"])
    assert values["tags"] == ["a", "b"]
    with pytest.raises(InputError) as exc:
        resolve_inputs(WF, cli_pairs=["issue=1", "issue=2", "issue=3", "base=x", "base=y"])
    assert [e for e in exc.value.errors if "more than once" in e] == [
        "--input: input 'issue' given more than once (only array inputs may be repeated)",
        "--input: input 'base' given more than once (only array inputs may be repeated)",
    ]
