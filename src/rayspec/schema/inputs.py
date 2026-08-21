# SPDX-License-Identifier: Apache-2.0
"""Workflow inputs: JSON-Schema-typed declarations compiled to one object schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import model_validator

from rayspec.schema.base import StrictModel

JsonType = Literal["string", "integer", "number", "boolean", "array", "object"]


class InputSpec(StrictModel):
    type: JsonType = "string"
    required: bool = False
    default: Any = None
    description: str | None = None
    enum: list[Any] | None = None
    items: dict[str, Any] | None = None
    properties: dict[str, Any] | None = None
    #: additive: the value is never persisted (``run.json``/``context.json``/events/``plan``/
    #: ``show`` print ``<secret>``) and reaches ``shell:``/``python:`` steps only as
    #: ``RAYSPEC_INPUT_<NAME>`` (or through their ``env:`` mapping); naming it anywhere else is a
    #: load-time error; every resume entry re-obtains it (``--input`` / the env var)
    secret: bool = False

    @classmethod
    def _what(cls) -> str:
        return "input"

    @model_validator(mode="after")
    def _required_without_default(self) -> InputSpec:
        if self.required and "default" in self.model_fields_set:
            raise ValueError("a required input cannot have a default (drop one of them)")
        if self.secret and "default" in self.model_fields_set:
            raise ValueError(
                "a secret input cannot have a default (a default would be persisted in the "
                "workflow file and in run.json)"
            )
        return self

    @property
    def has_default(self) -> bool:
        return "default" in self.model_fields_set

    def to_json_schema(self) -> dict[str, Any]:
        """The JSON-Schema fragment of this input (``secret`` is a rayspec marker, not schema)."""
        schema: dict[str, Any] = {"type": self.type}
        if self.description is not None:
            schema["description"] = self.description
        if self.enum is not None:
            schema["enum"] = self.enum
        if self.items is not None:
            schema["items"] = self.items
        if self.properties is not None:
            schema["properties"] = self.properties
        if self.has_default:
            schema["default"] = self.default
        return schema


def inputs_to_json_schema(inputs: Mapping[str, InputSpec]) -> dict[str, Any]:
    """Compile declared inputs into a single object schema (``additionalProperties: false``)."""
    return {
        "type": "object",
        "properties": {name: spec.to_json_schema() for name, spec in inputs.items()},
        "required": [name for name, spec in inputs.items() if spec.required],
        "additionalProperties": False,
    }


__all__ = ["InputSpec", "JsonType", "inputs_to_json_schema"]
