# SPDX-License-Identifier: Apache-2.0
"""StrictModel: ``extra="forbid"`` + did-you-mean for unknown keys + ``.parse()`` → SchemaError."""

from __future__ import annotations

import difflib
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from rayspec.schema.errors import SchemaError, schema_error_from_validation


def suggest(name: str, candidates: list[str] | set[str]) -> str | None:
    matches = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _field_names(cls: type[BaseModel]) -> set[str]:
    names: set[str] = set()
    for fname, finfo in cls.model_fields.items():
        names.add(fname)
        if finfo.alias:
            names.add(finfo.alias)
    return names


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=False)

    @classmethod
    def _what(cls) -> str:
        return cls.__name__

    @classmethod
    def _unknown_key_message(cls, key: str, data: dict[str, Any]) -> str:
        hint = suggest(key, _field_names(cls))
        base = f"unknown field {key!r} for {cls._what()}"
        return f"{base}; did you mean {hint!r}?" if hint else base

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            known = _field_names(cls)
            unknown = [k for k in data if isinstance(k, str) and k not in known]
            if unknown:
                messages = [cls._unknown_key_message(k, data) for k in unknown]
                # ``unknown_keys``/``unknown_messages`` are additive context: the joined
                # ``message`` stays the error's text, but the aggregation pass in
                # :mod:`rayspec.schema.errors` reads the keys from here instead of parsing the
                # rendered English back apart (which broke on keys containing quotes).
                raise PydanticCustomError(
                    "unknown_field",
                    "{message}",
                    {
                        "message": "; ".join(messages),
                        "unknown_keys": list(unknown),
                        "unknown_messages": messages,
                    },
                )
        return data

    @classmethod
    def parse(cls, data: Any, *, source: str | None = None) -> Self:
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise schema_error_from_validation(exc, data, source=source) from None


__all__ = ["SchemaError", "StrictModel", "suggest"]
