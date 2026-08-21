# SPDX-License-Identifier: Apache-2.0
"""SchemaError: human-readable, location-aware wrapper around pydantic validation errors.

Module boundary: turns one :class:`pydantic.ValidationError` into a list of
:class:`SchemaProblem` records — a field path, a message, an optional did-you-mean hint and
(when the caller knows the document's line map) the ``file:line`` the problem sits on. The
aggregation pass (:func:`expand_schema_errors`) recovers the problems that a rejected
unknown key masked, so ``rayspec validate`` reports every mistake of a document instead of the
first one. Nothing here reads files or renders output.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pydantic import ValidationError

from rayspec.errors import RayspecError

#: Step-kind tags (discriminator). They appear in error locations right after a list index (or at
#: position 0 for parse_step) and are NOT data keys, even though the step mapping has that key.
_KIND_TAGS = frozenset({"prompt", "shell", "python", "loop", "each", "approve", "include", "stop"})
#: Tags that can never be data keys.
_OTHER_TAGS = frozenset({"agent:name", "agent:override", "agent:inline"})

#: pydantic error type of the "unknown key" checks in :mod:`rayspec.schema.base` and
#: :mod:`rayspec.schema.steps`; the only problems :func:`expand_schema_errors` can prune.
UNKNOWN_FIELD = "unknown_field"

#: Both unknown-key checks join one message per offending key with ``"; "`` into a single pydantic
#: error whose location is the *model*, not the key, and carry the keys and their individual
#: messages in the error's ``ctx`` (``unknown_keys`` / ``unknown_messages``, see
#: ``StrictModel._reject_unknown_keys``). The split below is only the fallback for an
#: ``unknown_field`` error raised without that context; it is deliberately never used to decide
#: that a key may be PRUNED, because a key whose name contains a quote does not match it.
_UNKNOWN_KEY_SPLIT = re.compile(r"; (?=(?:unknown )?field '[^']+' )")
_UNKNOWN_KEY_NAME = re.compile(r"^(?:unknown )?field '([^']+)'")
_HINT_SPLIT = re.compile(r"; (did you mean '[^']+'\?)$")

#: Safety rails for the aggregation pass: how often the document may be re-parsed with the
#: rejected keys removed, and how many problems one document may report. The cap applies to the
#: FIRST pass too — one pydantic unknown-key error already expands into one problem per key — and
#: a truncated list ends with :func:`truncation_problem` so the report never pretends to be
#: complete.
MAX_AGGREGATION_PASSES = 6
MAX_PROBLEMS = 50


@dataclass(frozen=True, slots=True)
class SchemaProblem:
    """One schema problem of one document.

    ``field`` is the human path inside the document (``steps[1] (id: build).join``), ``loc`` the
    machine key path used to look the line up, ``kind`` the pydantic error type and ``hint`` the
    did-you-mean part when the message carried one.
    """

    field: str
    message: str
    loc: tuple[str | int, ...] = ()
    kind: str = ""
    hint: str | None = None
    line: int | None = None
    source: str | None = None

    @property
    def location(self) -> str | None:
        """``<source>:<line>`` when both are known, else ``None``."""
        if self.source is None or self.line is None:
            return None
        return f"{self.source}:{self.line}"

    def rendered(self) -> str:
        """``<source>:<line>: <field>: <message>``, dropping the parts that are unknown."""
        prefix = self.location or self.source
        head = f"{prefix}: " if prefix else ""
        field = f"{self.field}: " if self.field else ""
        return f"{head}{field}{self.message}"

    def text(self) -> str:
        """``<field>: <message>`` — the entry shape of :attr:`SchemaError.errors`."""
        return f"{self.field}: {self.message}" if self.field else self.message

    def to_json(self) -> dict[str, Any]:
        """The ``--json`` object of one problem (``path`` is filled by the CLI when unknown)."""
        return {
            "path": self.source,
            "line": self.line,
            "location": self.location,
            "field": self.field or None,
            "message": self.message,
            "hint": self.hint,
        }


class SchemaError(RayspecError, ValueError):
    """One or more schema problems, each rendered as ``<location>: <message>``.

    ``errors`` keeps the plain ``<field>: <message>`` entries every consumer has always seen;
    ``problems`` (additive) carries the same list with the key path, the did-you-mean hint
    and — once :func:`expand_schema_errors` has seen the document's line map — the line number.
    ``str(exc)`` prints one problem per line, with the line number when it is known, so every
    error boundary that only forwards the message (``rayspec plan``, ``run``, ``resume``) shows
    the same locations ``rayspec validate`` does.
    """

    def __init__(
        self,
        errors: Sequence[str],
        *,
        source: str | None = None,
        problems: Sequence[SchemaProblem] = (),
    ):
        self.errors = list(errors)
        self.source = source
        self.problems = tuple(problems)
        prefix = f"{source}: " if source else ""
        lines = (
            [p.rendered() for p in self.problems]
            if self.problems
            else [prefix + e for e in self.errors]
        )
        super().__init__("\n".join(lines), hint=None)


def _describe_loc(loc: Sequence[Any], data: Any) -> tuple[str, tuple[str | int, ...]]:
    """Render ``('steps', 1, 'shell', 'join')`` as ``steps[1] (id: bad).join`` + its data path."""
    parts: list[str] = []
    keys: list[str | int] = []
    cur = data
    for i, part in enumerate(loc):
        if isinstance(part, int):
            label = f"[{part}]"
            keys.append(part)
            if isinstance(cur, list | tuple) and 0 <= part < len(cur):
                cur = cur[part]
                if isinstance(cur, dict) and isinstance(cur.get("id"), str):
                    label += f" (id: {cur['id']})"
            else:
                cur = None
            if parts:
                parts[-1] += label
            else:
                parts.append(label)
            continue
        part_s = str(part)
        if part_s in _OTHER_TAGS:
            continue
        if part_s in _KIND_TAGS and (i == 0 or isinstance(loc[i - 1], int)):
            continue  # discriminator tag, not a key in the user's data
        if part_s == "[key]":
            continue  # pydantic marks dict-key validation errors with a synthetic "[key]" segment
        if isinstance(cur, dict) and part_s in cur:
            cur = cur[part_s]
            parts.append(part_s)
        else:
            cur = None
            parts.append(part_s)
        keys.append(part_s)
    return (".".join(parts) if parts else "<root>"), tuple(keys)


def _clean_message(err: Mapping[str, Any]) -> str:
    msg = str(err.get("msg", ""))
    for prefix in ("Value error, ", "Assertion failed, "):
        if msg.startswith(prefix):
            msg = msg[len(prefix) :]
    return msg


def _split_hint(message: str) -> tuple[str, str | None]:
    """Split a trailing ``; did you mean 'x'?`` off ``message`` (the message keeps it)."""
    match = _HINT_SPLIT.search(message)
    return (message, match.group(1) if match else None)


def _unknown_key_pairs(err: Mapping[str, Any], message: str) -> list[tuple[str | None, str]]:
    """``(key, message)`` per offending key of one ``unknown_field`` error.

    The keys come from the error's ``ctx`` (``unknown_keys``/``unknown_messages``). Only if that
    context is missing does the message get split by pattern, and then the key stays ``None`` —
    an unidentified key must never be pruned from the document.
    """
    ctx = err.get("ctx")
    if isinstance(ctx, Mapping):
        keys = ctx.get("unknown_keys")
        messages = ctx.get("unknown_messages")
        if (
            isinstance(keys, Sequence)
            and isinstance(messages, Sequence)
            and not isinstance(keys, str | bytes)
            and not isinstance(messages, str | bytes)
            and len(keys) == len(messages)
            and keys
        ):
            return [
                (key if isinstance(key, str) else None, str(text))
                for key, text in zip(keys, messages, strict=True)
            ]
    out: list[tuple[str | None, str]] = []
    for part in _UNKNOWN_KEY_SPLIT.split(message):
        name = _UNKNOWN_KEY_NAME.match(part)
        out.append((name.group(1) if name else None, part))
    return out


def _unknown_key_problems(
    err: Mapping[str, Any], message: str, field: str, loc: tuple[str | int, ...]
) -> list[SchemaProblem]:
    """One problem per offending key of a joined unknown-field message (see the module docstring).

    The parent's ``field``/``loc`` is extended by the key so the problem points at the line the
    key sits on rather than at the whole mapping. A key that could not be identified keeps the
    parent's location and an EMPTY ``kind``, so :func:`expand_schema_errors` never prunes (and
    thereby silences) the mapping it sits in.
    """
    out: list[SchemaProblem] = []
    for key, part in _unknown_key_pairs(err, message):
        text, hint = _split_hint(part)
        if key is None:
            out.append(SchemaProblem(field=field, message=text, loc=loc, hint=hint))
            continue
        child = f"{field}.{key}" if field != "<root>" else key
        out.append(
            SchemaProblem(
                field=child,
                message=text,
                loc=(*loc, key),
                kind=UNKNOWN_FIELD,
                hint=hint,
            )
        )
    return out


def problems_from_validation(exc: ValidationError, data: Any) -> list[SchemaProblem]:
    """Every problem of ``exc``, de-duplicated, in pydantic's order."""
    out: list[SchemaProblem] = []
    seen: set[tuple[str, str]] = set()
    for err in exc.errors(include_url=False):
        field, loc = _describe_loc(err.get("loc", ()), data)
        message = _clean_message(err)
        kind = str(err.get("type", ""))
        if kind == UNKNOWN_FIELD:
            candidates = _unknown_key_problems(err, message, field, loc)
        else:
            text, hint = _split_hint(message)
            candidates = [SchemaProblem(field=field, message=text, loc=loc, kind=kind, hint=hint)]
        for problem in candidates:
            key = (problem.field, problem.message)
            if key not in seen:
                seen.add(key)
                out.append(problem)
    return out


def schema_error_from_validation(
    exc: ValidationError, data: Any, *, source: str | None = None
) -> SchemaError:
    """Build the :class:`SchemaError` of one failed ``model_validate`` call."""
    problems = [replace(p, source=source) for p in problems_from_validation(exc, data)]
    return SchemaError([p.text() for p in problems], source=source, problems=problems)


def line_of(lines: Mapping[tuple[str | int, ...], int], loc: Sequence[str | int]) -> int | None:
    """Line of ``loc`` in a :data:`~rayspec.loader.yaml.LineMap`, or of its nearest ancestor.

    The walk stops ABOVE the document root: a non-empty ``loc`` that is nowhere in the map has no
    known line and returns ``None`` rather than the root's line 1 — a confidently wrong location
    is worse than a missing one, because an editor jumps to it.
    """
    keys = tuple(loc)
    if not keys:
        return lines.get(())
    while keys:
        line = lines.get(keys)
        if line is not None:
            return line
        keys = keys[:-1]
    return None


def _pruned(data: Any, locs: Iterable[Sequence[str | int]]) -> Any:
    """A deep copy of ``data`` without the keys at ``locs`` (missing ones are ignored)."""
    out = copy.deepcopy(data)
    for loc in locs:
        cur: Any = out
        for part in loc[:-1]:
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
                continue
            if isinstance(cur, list) and isinstance(part, int) and 0 <= part < len(cur):
                cur = cur[part]
                continue
            cur = None
            break
        last = loc[-1]
        if isinstance(cur, dict) and last in cur:
            del cur[last]
    return out


def truncation_problem(dropped: int, *, source: str | None = None) -> SchemaProblem:
    """The final entry of a truncated report: how many problems are NOT shown."""
    return SchemaProblem(
        field="",
        message=f"… and {dropped} more problems (showing the first {MAX_PROBLEMS})",
        source=source,
    )


def expand_schema_errors(
    error: SchemaError,
    data: Any,
    parse: Callable[..., Any],
    *,
    lines: Mapping[tuple[str | int, ...], int] | None = None,
    max_passes: int = MAX_AGGREGATION_PASSES,
) -> SchemaError:
    """Collect every schema problem of one document, not just the first.

    An unknown key is rejected *before* pydantic validates the rest of that mapping, so a single
    typo at the top level used to hide every other mistake in the file. This re-parses the
    document with the rejected keys removed until nothing new appears (bounded by ``max_passes``
    and :data:`MAX_PROBLEMS`), then stamps every problem with its ``file:line`` from ``lines``.

    ``parse`` is the same callable that produced ``error`` (``parse_workflow``,
    ``parse_agent_def``, …); it is called as ``parse(data, source=error.source)`` and must raise
    :class:`SchemaError`. Any other failure ends the pass — aggregation is best effort and never
    turns a reported problem into a crash.
    """
    seed = list(error.problems) or [
        SchemaProblem(field="", message=text, source=error.source) for text in error.errors
    ]
    problems = seed[:MAX_PROBLEMS]
    dropped = len(seed) - len(problems)
    seen = {(p.field, p.message) for p in seed}
    latest = seed
    work = data
    for _ in range(max_passes):
        removable = [p.loc for p in latest if p.kind == UNKNOWN_FIELD and p.loc]
        if not removable or len(problems) >= MAX_PROBLEMS:
            break
        work = _pruned(work, removable)
        try:
            parse(work, source=error.source)
        except SchemaError as exc:
            latest = list(exc.problems)
        except Exception:  # best effort: report what we already have
            break
        else:
            break
        for problem in latest:
            key = (problem.field, problem.message)
            if key in seen:
                continue
            seen.add(key)
            if len(problems) >= MAX_PROBLEMS:
                dropped += 1
                continue
            problems.append(problem)
    if lines is not None:
        problems = [replace(p, line=line_of(lines, p.loc)) for p in problems]
    problems = [replace(p, source=error.source) for p in problems]
    problems.sort(key=lambda p: (p.line is None, p.line or 0))
    if dropped:
        problems.append(truncation_problem(dropped, source=error.source))
    return SchemaError([p.text() for p in problems], source=error.source, problems=problems)


def join_errors(errors: Iterable[str], *, source: str | None = None) -> SchemaError:
    return SchemaError(list(errors), source=source)


__all__ = [
    "MAX_AGGREGATION_PASSES",
    "MAX_PROBLEMS",
    "UNKNOWN_FIELD",
    "SchemaError",
    "SchemaProblem",
    "expand_schema_errors",
    "join_errors",
    "line_of",
    "problems_from_validation",
    "schema_error_from_validation",
    "truncation_problem",
]
