"""An invalid value for a required input is one problem (invalid), never also 'missing'."""

from __future__ import annotations

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
            "target": {"type": "string", "default": "."},
            "ratio": {"type": "number", "required": True},
        },
        "steps": [{"id": "a", "shell": "echo"}],
    }
)


def test_invalid_required_input_is_reported_once() -> None:
    with pytest.raises(InputError) as ei:
        resolve_inputs(WF, cli_pairs=["issue=abc", "ratio=1.5"], env={})
    exc = ei.value
    assert exc.errors == ["input 'issue': expected an integer, got 'abc'"]
    assert exc.problems == {"issue": ["expected an integer, got 'abc'"]} or exc.problems == {
        "issue": ["input 'issue': expected an integer, got 'abc'"]
    }
    assert "missing" not in str(exc)
    assert exc.partial == {"ratio": 1.5, "target": "."}


def test_invalid_env_value_for_required_input_is_reported_once() -> None:
    with pytest.raises(InputError) as ei:
        resolve_inputs(WF, cli_pairs=["ratio=2"], env={"RAYSPEC_INPUT_ISSUE": "x"})
    assert len(ei.value.errors) == 1
    assert "RAYSPEC_INPUT_ISSUE" in ei.value.errors[0]
    assert "missing" not in str(ei.value)


def test_unknown_name_keeps_did_you_mean_next_to_an_invalid_value() -> None:
    with pytest.raises(InputError) as ei:
        resolve_inputs(WF, cli_pairs=["issue=abc", "nope=1", "ratio=1"], env={})
    msg = str(ei.value)
    assert "--input: unknown input 'nope' (declared: issue, target, ratio)" in msg
    assert "missing required input(s)" not in msg
    with pytest.raises(InputError, match="did you mean 'issue'"):
        resolve_inputs(WF, cli_pairs=["isue=1", "ratio=1"], env={})


def test_truly_missing_required_inputs_are_still_reported() -> None:
    with pytest.raises(InputError, match=r"missing required input\(s\): issue, ratio"):
        resolve_inputs(WF, env={})
    with pytest.raises(InputError) as ei:
        resolve_inputs(WF, cli_pairs=["issue=abc"], env={})
    assert "missing required input(s): ratio (" in str(ei.value)
    assert ei.value.problems["ratio"] == ["missing (required)"]
    assert "missing" not in " ".join(ei.value.problems["issue"])
