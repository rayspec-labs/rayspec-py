"""``rayspec.providers._schema.for_openai_strict``: OpenAI strict-mode schema normalisation."""

from __future__ import annotations

import copy

from rayspec.providers._schema import for_openai_strict


def test_adds_additional_properties_false_and_requires_every_property():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }
    strict, warnings = for_openai_strict(schema)
    assert strict["additionalProperties"] is False
    assert strict["required"] == ["a", "b"]
    assert warnings == []  # an already closed set of properties is not a warning


def test_input_is_not_mutated_and_nested_objects_recurse():
    schema = {
        "type": "object",
        "properties": {
            "meta": {
                "type": "object",
                "properties": {"x": {"type": "number"}},
            },
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        },
    }
    before = copy.deepcopy(schema)
    strict, _ = for_openai_strict(schema)
    assert schema == before
    meta = strict["properties"]["meta"]
    assert meta["additionalProperties"] is False and meta["required"] == ["x"]
    item = strict["properties"]["items"]["items"]
    assert item["additionalProperties"] is False and item["required"] == ["id"]
    assert strict["required"] == ["meta", "items"]


def test_warns_when_closing_an_open_record():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": True,
    }
    strict, warnings = for_openai_strict(schema)
    assert strict["additionalProperties"] is False
    assert len(warnings) == 1 and "additionalProperties" in warnings[0] and "$" in warnings[0]

    open_map = {"type": "object", "additionalProperties": {"type": "string"}}
    strict2, warnings2 = for_openai_strict(open_map)
    assert strict2["additionalProperties"] is False
    assert len(warnings2) == 1


def test_recurses_into_defs_combinators_and_required_is_preserved_order():
    schema = {
        "$defs": {"Inner": {"type": "object", "properties": {"v": {"type": "boolean"}}}},
        "definitions": {"Old": {"type": "object", "properties": {"w": {"type": "boolean"}}}},
        "type": "object",
        "properties": {
            "one": {"anyOf": [{"$ref": "#/$defs/Inner"}, {"type": "null"}]},
            "two": {"oneOf": [{"type": "object", "properties": {"z": {"type": "string"}}}]},
            "three": {"allOf": [{"type": "object", "properties": {"q": {"type": "string"}}}]},
        },
        "required": ["two"],
    }
    strict, _ = for_openai_strict(schema)
    assert strict["$defs"]["Inner"]["required"] == ["v"]
    assert strict["$defs"]["Inner"]["additionalProperties"] is False
    assert strict["definitions"]["Old"]["required"] == ["w"]
    assert strict["properties"]["two"]["oneOf"][0]["required"] == ["z"]
    assert strict["properties"]["three"]["allOf"][0]["additionalProperties"] is False
    # existing required entries keep their position, the rest follow in property order
    assert strict["required"] == ["two", "one", "three"]


def test_object_without_properties_gets_closed_and_no_required_key():
    strict, _ = for_openai_strict({"type": "object"})
    assert strict == {"type": "object", "additionalProperties": False}


def test_non_object_schema_passes_through_unchanged():
    strict, warnings = for_openai_strict({"type": "string", "enum": ["a", "b"]})
    assert strict == {"type": "string", "enum": ["a", "b"]} and warnings == []
