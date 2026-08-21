# SPDX-License-Identifier: Apache-2.0
"""The three rayspec filters: ``fromjson``, ``regex_search``, ``has_signal``.

Growth policy (constitution, principle 7): every Jinja builtin filter/test is kept, and a new
filter is added only if it (a) cannot be written in one line from builtins, (b) is pure, total
and deterministic (no IO) and (c) *shapes* data rather than *judges* it. Anything else is a
``python:`` step that emits structured output the workflow can gate on.

Module boundary: depends on :mod:`rayspec.templating.undefined` only. Filters raise
``ValueError`` with a message naming the fix; the engine converts every exception raised while
rendering into :class:`rayspec.templating.errors.TemplateRenderError`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from rayspec.templating.undefined import RayspecUndefined

_SIGNAL_STRIP = " \t\r\n*_`"


def fromjson(value: Any) -> Any:
    """Parse a JSON document. ``"{}" | fromjson`` → ``{}``; non-text input is an error."""
    if isinstance(value, Mapping | list):
        raise ValueError(
            "fromjson: value is already structured (dict/list) — access fields directly"
        )
    if not isinstance(value, str):
        raise ValueError(f"fromjson: expected a JSON string, got {type(value).__name__}")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"fromjson: invalid JSON ({exc.msg} at line {exc.lineno})") from exc


def regex_search(value: Any, pattern: str, group: int | str = 0) -> Any:
    """``re.search`` over a text value; returns the group or an undefined when there is no match.

    The undefined chains and supports ``| default(...)`` / ``is defined`` but raises on use, so
    a missing match never silently becomes ``''``.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"regex_search: expected a text value, got {type(value).__name__} "
            "(structured output: select the field first)"
        )
    try:
        match = re.search(pattern, value)
    except re.error as exc:
        raise ValueError(f"regex_search: invalid pattern {pattern!r}: {exc}") from exc
    if match is None:
        return RayspecUndefined(
            hint=f"regex_search: pattern {pattern!r} did not match",
            rayspec_hint="use | default(...) or guard with `is defined`",
        )
    try:
        return match.group(group)
    except IndexError as exc:
        raise ValueError(f"regex_search: no such group {group!r}") from exc


def has_signal(value: Any, name: str) -> bool:
    """True iff ``name`` occurs as a signal in text output (case-sensitive).

    A signal is either a whole line equal to ``name`` after stripping whitespace and surrounding
    ``*``, ``_`` and backticks (markdown emphasis), or an explicit ``<signal>name</signal>`` tag
    anywhere. ``"not DONE yet"`` is *not* a signal. Structured (dict/list) input is an error —
    compare the field instead (``output.field == ...``).
    """
    if isinstance(value, Mapping | list):
        raise ValueError(
            "has_signal: output is structured (dict/list); use output.field == ... instead"
        )
    if not isinstance(value, str):
        raise ValueError(f"has_signal: expected a text value, got {type(value).__name__}")
    if not isinstance(name, str) or not name:
        raise ValueError("has_signal: the signal name must be a non-empty string")
    for line in value.splitlines():
        if line.strip(_SIGNAL_STRIP) == name:
            return True
    tag = re.compile(r"<signal>\s*" + re.escape(name) + r"\s*</signal>")
    return tag.search(value) is not None


FILTERS: dict[str, Any] = {
    "fromjson": fromjson,
    "regex_search": regex_search,
    "has_signal": has_signal,
}
"""Filters registered on every rayspec environment (in addition to all Jinja builtins)."""

TESTS: dict[str, Any] = {"has_signal": has_signal}
"""``value is has_signal("DONE")`` reads naturally in ``when:``/``until:`` expressions."""

__all__ = ["FILTERS", "TESTS", "fromjson", "has_signal", "regex_search"]
