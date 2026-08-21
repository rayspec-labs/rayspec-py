# SPDX-License-Identifier: Apache-2.0
"""Structured output (``output_schema``) lives in the engine, not in the adapters.

* ``enforced`` providers receive the schema in the request and return ``AgentResult.structured``;
  the engine validates it with ``jsonschema`` and re-asks **once** (via ``resume_session``,
  quoting the validation error) before failing.
* ``best_effort`` providers get a "respond ONLY with JSON matching this schema" suffix; the
  engine extracts fenced / balanced JSON from the text, validates, and re-asks at most twice.
* ``none`` is rejected at load time (capability check) — here it is a :class:`ValueError`.

Module boundary: provider-neutral; depends on :mod:`rayspec.providers.base` types only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import jsonschema

from rayspec.providers.base import AgentRequest, AgentResult, EmitFn, Provider, Usage

MAX_REASKS: dict[str, int] = {"enforced": 1, "best_effort": 2}

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)


def schema_suffix(schema: Mapping[str, Any]) -> str:
    """The best-effort instruction appended to the prompt."""
    return (
        "\n\nRespond ONLY with a JSON document (no prose, no code fences) that matches this "
        "JSON Schema exactly:\n" + json.dumps(schema, indent=2, ensure_ascii=False)
    )


def reask_prompt(error: str, schema: Mapping[str, Any]) -> str:
    """The follow-up turn after an invalid structured answer."""
    return (
        "Your previous response was not valid structured output: "
        f"{error}\nRespond again with ONLY a JSON document matching this JSON Schema:\n"
        + json.dumps(schema, indent=2, ensure_ascii=False)
    )


def extract_json(text: str) -> Any:
    """First JSON document in ``text``: whole text → fenced block → balanced ``{}``/``[]`` span.

    Raises ``ValueError`` when nothing parses.
    """
    stripped = text.strip()
    if stripped:
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    for match in _FENCE_RE.finditer(text):
        try:
            return json.loads(match.group(1))
        except ValueError:
            continue
    for candidate in _balanced_spans(text):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ValueError("no JSON document found in the response")


def _balanced_spans(text: str):
    """Yield balanced ``{...}`` / ``[...]`` spans (outermost first), string-aware."""
    openers = {"{": "}", "[": "]"}
    for start, ch in enumerate(text):
        if ch not in openers:
            continue
        depth = 0
        in_str = False
        escape = False
        for end in range(start, len(text)):
            c = text[end]
            if in_str:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c in openers:
                depth += 1
            elif c in ("}", "]"):
                depth -= 1
                if depth == 0:
                    yield text[start : end + 1]
                    break


def validate_value(value: Any, schema: Mapping[str, Any]) -> str | None:
    """``None`` when ``value`` matches ``schema``, else the validation message."""
    try:
        jsonschema.validate(value, dict(schema))
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path)
        return f"{exc.message}" + (f" (at {where})" if where else "")
    except jsonschema.SchemaError as exc:
        return f"invalid output_schema: {exc.message}"
    return None


def candidate_value(result: AgentResult, mode: str) -> tuple[Any, str | None]:
    """The structured value of a result (enforced: ``structured``, falling back to the text)."""
    if mode == "enforced" and result.structured is not None:
        return result.structured, None
    try:
        return extract_json(result.text), None
    except ValueError as exc:
        if mode == "enforced":
            return None, "the provider returned no structured output"
        return None, str(exc)


@dataclass(slots=True)
class StructuredResult:
    """Outcome of :func:`run_structured`: the last provider result and the validated value."""

    result: AgentResult
    value: Any = None
    error: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None
    calls: int = 1


async def run_structured(
    provider: Provider,
    req: AgentRequest,
    emit: EmitFn,
    schema: Mapping[str, Any],
    *,
    mode: str | None = None,
) -> StructuredResult:
    """Run ``req`` with structured output enforcement + re-asks (see module docstring)."""
    mode = mode or provider.capabilities.structured_output
    if mode not in MAX_REASKS:
        raise ValueError(f"provider {provider.id!r} does not support structured output")
    if mode == "enforced":
        req = replace(req, output_schema=dict(schema))
    else:
        req = replace(req, output_schema=None, prompt=req.prompt + schema_suffix(schema))
    result = await provider.run(req, emit)
    total = StructuredResult(result=result, usage=result.usage, cost_usd=result.cost_usd)
    reasks = 0
    while True:
        if result.status != "success":
            total.result = result
            return total
        value, error = candidate_value(result, mode)
        if error is None:
            error = validate_value(value, schema)
        if error is None:
            total.value = value
            total.error = None
            total.result = result
            return total
        total.error = error
        if reasks >= MAX_REASKS[mode]:
            total.result = result
            return total
        reasks += 1
        follow_up = replace(
            req,
            prompt=reask_prompt(error, schema),
            resume_session=result.session_ref or req.resume_session,
            fork_session=False,
            step_attempt=req.step_attempt,
        )
        result = await provider.run(follow_up, emit)
        total.calls += 1
        total.usage = total.usage + result.usage
        if result.cost_usd is not None:
            total.cost_usd = (total.cost_usd or 0.0) + result.cost_usd


__all__ = [
    "MAX_REASKS",
    "StructuredResult",
    "candidate_value",
    "extract_json",
    "reask_prompt",
    "run_structured",
    "schema_suffix",
    "validate_value",
]
