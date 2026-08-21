# SPDX-License-Identifier: Apache-2.0
"""JSON-schema normalisation for OpenAI *strict* structured output.

Boundary: pure data transformation, no SDK imports. The Codex adapter calls
:func:`for_openai_strict` before handing ``output_schema`` to the SDK; the engine keeps validating
the model's answer against the **original** schema (``engine/structured.py``).

OpenAI strict mode requires every object schema to carry ``additionalProperties: false`` and to
list *all* of its properties in ``required``. This module applies both rules recursively
(``properties``, ``items``/``prefixItems``, ``$defs``/``definitions``, ``anyOf``/``oneOf``/
``allOf``, ``not``, ``if``/``then``/``else``, ``additionalProperties``/``patternProperties``).

Only these two rules are applied. Other keywords OpenAI strict mode may reject (``format``,
``pattern``, ``minimum``/``maximum``, ``minLength``/``maxLength``, ``default``, ...) pass through
unchanged — the supported list changes between API releases — and surface as a ``badRequest``
turn error on the step (documented in CONTRACTS next to ``output_schema``).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

_SUBSCHEMA_KEYS = ("items", "not", "if", "then", "else", "contains", "propertyNames")
_SUBSCHEMA_LIST_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUBSCHEMA_MAP_KEYS = (
    "properties",
    "$defs",
    "definitions",
    "patternProperties",
    "dependentSchemas",
)


def for_openai_strict(schema: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return ``(strict_schema, warnings)`` for ``schema`` (the input is never mutated).

    Every object schema gets ``additionalProperties: false`` and a ``required`` list naming all of
    its ``properties`` (existing entries keep their order, missing ones follow in declaration
    order). A warning is recorded for every object whose ``additionalProperties`` was *open*
    (``true`` or a sub-schema) because closing it changes what the model may return. Non-object
    schemas pass through unchanged.
    """
    strict = copy.deepcopy(dict(schema))
    warnings: list[str] = []
    _walk(strict, "$", warnings)
    return strict, warnings


def _walk(node: Any, path: str, warnings: list[str]) -> None:
    if isinstance(node, list):
        for i, item in enumerate(node):
            _walk(item, f"{path}[{i}]", warnings)
        return
    if not isinstance(node, dict):
        return
    if _is_object(node):
        _close_object(node, path, warnings)
    for key in _SUBSCHEMA_KEYS:
        if key in node:
            _walk(node[key], f"{path}.{key}", warnings)
    for key in _SUBSCHEMA_LIST_KEYS:
        value = node.get(key)
        if isinstance(value, list):
            for i, item in enumerate(value):
                _walk(item, f"{path}.{key}[{i}]", warnings)
    for key in _SUBSCHEMA_MAP_KEYS:
        value = node.get(key)
        if isinstance(value, dict):
            for name, sub in value.items():
                _walk(sub, f"{path}.{key}.{name}", warnings)
    extra = node.get("additionalProperties")
    if isinstance(extra, dict):  # non-object schemas may still carry a sub-schema here
        _walk(extra, f"{path}.additionalProperties", warnings)


def _is_object(node: dict[str, Any]) -> bool:
    typ = node.get("type")
    if typ == "object":
        return True
    if isinstance(typ, list) and "object" in typ:
        return True
    return typ is None and isinstance(node.get("properties"), dict)


def _close_object(node: dict[str, Any], path: str, warnings: list[str]) -> None:
    extra = node.get("additionalProperties", True)
    if extra is not False:
        if "additionalProperties" in node:
            warnings.append(
                f"{path}: additionalProperties was open ({_describe(extra)}); OpenAI strict mode "
                "requires additionalProperties: false — closed it (unlisted keys will be rejected)"
            )
        node["additionalProperties"] = False
    props = node.get("properties")
    if isinstance(props, dict) and props:
        existing = node.get("required")
        required: list[str] = [r for r in existing if isinstance(r, str)] if existing else []
        seen = set(required)
        for name in props:
            if name not in seen:
                required.append(name)
                seen.add(name)
        node["required"] = required


def _describe(value: Any) -> str:
    if value is True:
        return "true"
    if isinstance(value, dict):
        return "a sub-schema"
    return repr(value)


__all__ = ["for_openai_strict"]
