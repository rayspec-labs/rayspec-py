"""``inputs.<name>.secret: true`` (additive schema field)."""

from __future__ import annotations

import pytest

from rayspec.schema import InputSpec, SchemaError, inputs_to_json_schema, parse_workflow


def test_secret_defaults_false_and_is_accepted():
    assert InputSpec.parse({}).secret is False
    spec = InputSpec.parse({"type": "string", "secret": True, "required": True})
    assert spec.secret is True and spec.required is True


def test_secret_with_default_is_a_load_error():
    with pytest.raises(SchemaError, match="secret"):
        InputSpec.parse({"secret": True, "default": "x"})


def test_secret_is_not_part_of_the_json_schema():
    schema = inputs_to_json_schema({"token": InputSpec.parse({"secret": True})})
    assert "secret" not in schema["properties"]["token"]


def test_workflow_with_secret_input_parses():
    wf = parse_workflow(
        {
            "rayspec": 1,
            "name": "sec",
            "inputs": {"token": {"type": "string", "secret": True}},
            "steps": [{"id": "a", "shell": "echo"}],
        }
    )
    assert wf.inputs["token"].secret is True
