# SPDX-License-Identifier: Apache-2.0
"""Stub provider: scripted, deterministic :class:`~rayspec.providers.base.Provider`.

Boundary: no SDK, no network. Used as the engine's test double and by ``rayspec run --dry-run``
(``--stubs <file>``). Behaviour is driven by a :class:`StubScript` (YAML or dict)::

    steps:                       # key = exact step path or fnmatch glob (``build[*]/implement``)
      review: {text: "LGTM"}
      assess: {output: {severity: high}}          # dict → structured + text=json
      flaky:                                       # fail n times, then succeed
        fail: {kind: api, message: boom, transient: true, times: 2}
        text: recovered
      build[*]/implement:
        sequence: ["first", {text: second}, {output: {ok: true}}]   # nth call; last repeats
        events:
          - {tool_call: {name: Bash, call_id: c1, input: {cmd: ls}}}
          - {tool_result: {call_id: c1, text: "a.py"}}
        usage: {input: 120, output: 40}
        latency_ms: 5
      audit:
        text: "clean"
        expect:                                    # assertions about the AgentRequest
          prompt_contains: ["parser.py", "def parse"]
          not_contains: "{{"                       # a template that did not render
          model: claude-sonnet-4-5
          access: read-only
          output_schema: true
          session: resumed
    match:                       # consulted after steps; first regex (search) that hits wins
      - {prompt_regex: "fix (the )?bug", text: "patched"}
    defaults: {latency_ms: 0, usage: {input: 100, output: 50}}

An ``expect:`` block turns a dry run from "the graph executed" into "the agent received the right
thing": every set field is asserted against the request, and a mismatch fails the step with
``AgentResult(status="error", error.kind="stub_expectation")`` and an excerpt of the rendered
prompt. It may sit on an entry or on a single ``sequence`` item (the item's block replaces the
entry's for that call). Assertions are checked before latency, before ``fail:`` and before the
scripted answer.

Resolution order per call: exact ``steps`` key → glob (``StepPath.matches``, declaration order
when several globs match) → ``match[]`` → default (``"[stub] " + prompt[:80]`` or, with
``output_schema``, a minimal schema instance).

Two call counters exist. The *per-entry* counter drives ``sequence`` (the n-th call that resolved
to that entry, across every path a glob matches — so ``build[*]/implement`` advances over loop
iterations). The *per-path* counter drives ``session_ref`` (``stub:<step_path>:<n>``) and
``fail.times`` (retry semantics are per step). Usage is deterministic (scripted, defaults, else
``len // 4``). Every request is recorded in :attr:`StubProvider.calls`. When a scripted latency
exceeds ``req.timeout_s`` the stub returns ``status="timeout"`` after half the timeout (the engine
owns the real deadline); without ``timeout_s`` it simply sleeps, so engine cancellation can be
tested through latency alone.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, cast, get_args

import anyio
import yaml

from rayspec import __version__
from rayspec.engine.paths import StepPath
from rayspec.errors import RayspecError
from rayspec.providers.base import (
    AccessLevel,
    AgentError,
    AgentEvent,
    AgentRequest,
    AgentResult,
    EmitFn,
    ErrorKind,
    EventKind,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
    ResultStatus,
    Usage,
)
from rayspec.providers.capabilities import STUB_CAPABILITIES

_EVENT_KINDS: frozenset[str] = frozenset(get_args(EventKind))
_ERROR_KINDS: frozenset[str] = frozenset(get_args(ErrorKind))
_RESULT_STATUSES: frozenset[str] = frozenset(get_args(ResultStatus))
_OUTCOME_KEYS = frozenset(
    {"output", "text", "fail", "events", "usage", "latency_ms", "status", "expect"}
)
_ENTRY_KEYS = _OUTCOME_KEYS | {"sequence"}
_EXPECT_KEYS = frozenset(
    {
        "prompt_regex",
        "prompt_contains",
        "not_contains",
        "access",
        "model",
        "output_schema",
        "session",
    }
)
_ACCESS_LEVELS: frozenset[str] = frozenset(level.value for level in AccessLevel)
_SESSION_EXPECTATIONS = frozenset({"resumed", "fresh"})
#: Characters of the rendered prompt shown next to a failed expectation.
EXCERPT_LIMIT = 600
_MATCH_KEYS = _ENTRY_KEYS | {"prompt_regex"}
_USAGE_KEYS = frozenset({"input", "cached_input", "cache_write", "output", "reasoning"})
_FAIL_KEYS = frozenset({"kind", "message", "transient", "times", "raise"})
_EVENT_FIELDS = frozenset({"kind", "text", "name", "call_id", "data", "nested"})
_WORD_RE = re.compile(r"\S+\s*|\s+")


class StubScriptError(RayspecError):
    """The stub script is malformed (unknown key, wrong type, bad regex…)."""


# --------------------------------------------------------------------------------------------------
# Script model
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StubFailure:
    """Scripted failure. ``times=None`` fails every call; ``raise_error`` raises ProviderError."""

    kind: ErrorKind = "api"
    message: str = "stub failure"
    transient: bool = False
    times: int | None = None
    raise_error: bool = False

    def applies(self, n: int) -> bool:
        return self.times is None or n <= self.times


def prompt_excerpt(prompt: str, *, around: int | None = None, limit: int = EXCERPT_LIMIT) -> str:
    """The part of the rendered prompt worth showing next to a failed expectation.

    Short prompts are shown whole; a long one is cut to ``limit`` characters — centred on
    ``around`` (the offset of an unwanted match) when given, else head + tail with an elision
    marker. Line breaks are kept: the point is to see what the agent actually received.
    """
    if len(prompt) <= limit:
        return prompt
    if around is not None:
        start = max(0, around - limit // 2)
        end = min(len(prompt), start + limit)
        head = "…" if start > 0 else ""
        tail = "…" if end < len(prompt) else ""
        return f"{head}{prompt[start:end]}{tail}"
    half = limit // 2
    return f"{prompt[:half]}\n… [{len(prompt) - limit} characters elided] …\n{prompt[-half:]}"


@dataclass(frozen=True, slots=True)
class StubExpect:
    """Assertions about the :class:`~rayspec.providers.base.AgentRequest` an entry answers.

    A dry run proves the graph executed; these prove the *agent was asked the right thing* — a
    prompt that rendered empty, an agent that silently ran on the wrong model or access level, a
    ``session:`` that started fresh. Every unset field is simply not checked.
    """

    prompt_regex: re.Pattern[str] | None = None
    prompt_contains: tuple[str, ...] = ()
    not_contains: tuple[str, ...] = ()
    access: str | None = None
    model: str | None = None
    output_schema: bool | None = None
    session: str | None = None  # "resumed" | "fresh"

    def check(self, req: AgentRequest) -> tuple[tuple[str, ...], int | None]:
        """``(reasons, offset)`` — every mismatch as one human sentence, plus the prompt offset
        to centre the excerpt on (the position of an unwanted match), if any."""
        reasons: list[str] = []
        offset: int | None = None
        if self.prompt_regex is not None and self.prompt_regex.search(req.prompt) is None:
            reasons.append(f"prompt_regex: /{self.prompt_regex.pattern}/ does not match the prompt")
        for needle in self.prompt_contains:
            if needle not in req.prompt:
                reasons.append(f"prompt_contains: {needle!r} is not in the prompt")
        for needle in self.not_contains:
            found = req.prompt.find(needle)
            if found >= 0:
                offset = found if offset is None else offset
                reasons.append(f"not_contains: {needle!r} occurs in the prompt at offset {found}")
        if self.access is not None and str(req.access) != self.access:
            reasons.append(f"access: expected {self.access!r}, got {str(req.access)!r}")
        if self.model is not None and (req.model or "") != self.model:
            reasons.append(f"model: expected {self.model!r}, got {(req.model or None)!r}")
        if self.output_schema is not None:
            has_schema = req.output_schema is not None
            if has_schema != self.output_schema:
                want = "an output_schema" if self.output_schema else "no output_schema"
                got = "one" if has_schema else "none"
                reasons.append(f"output_schema: expected {want}, got {got}")
        if self.session is not None:
            resumed = req.resume_session is not None
            if (self.session == "resumed") != resumed:
                got = f"resumed ({req.resume_session})" if resumed else "fresh"
                reasons.append(f"session: expected a {self.session} session, got {got}")
        return tuple(reasons), offset

    def failure_message(self, req: AgentRequest, reasons: Sequence[str], offset: int | None) -> str:
        """The loud one-block message: what did not match, then the rendered prompt excerpt."""
        head = f"stub expectation failed at {req.step_path}"
        body = "\n".join(f"  - {reason}" for reason in reasons)
        excerpt = prompt_excerpt(req.prompt, around=offset)
        quoted = "\n".join(f"  | {line}" for line in excerpt.splitlines() or [""])
        return f"{head}:\n{body}\nprompt as rendered ({len(req.prompt)} chars):\n{quoted}"


@dataclass(frozen=True, slots=True)
class StubOutcome:
    """One scripted response. Unset fields fall back to the entry base / script defaults."""

    text: str | None = None
    output: Any = None
    has_output: bool = False
    fail: StubFailure | None = None
    events: tuple[AgentEvent, ...] = ()
    usage: Usage | None = None
    latency_ms: float | None = None
    status: ResultStatus | None = None
    expect: StubExpect | None = None

    def merged_over(self, base: StubOutcome) -> StubOutcome:
        """Fill unset fields from ``base`` (sequence item over entry-level values)."""
        return StubOutcome(
            text=self.text if self.text is not None else base.text,
            output=self.output if self.has_output else base.output,
            has_output=self.has_output or base.has_output,
            fail=self.fail if self.fail is not None else base.fail,
            events=self.events or base.events,
            usage=self.usage if self.usage is not None else base.usage,
            latency_ms=self.latency_ms if self.latency_ms is not None else base.latency_ms,
            status=self.status if self.status is not None else base.status,
            expect=self.expect if self.expect is not None else base.expect,
        )


@dataclass(frozen=True, slots=True)
class StubEntry:
    """A ``steps:`` entry (``key`` = path or glob) or a ``match:`` entry (``prompt_regex``)."""

    key: str
    outcome: StubOutcome = field(default_factory=StubOutcome)
    sequence: tuple[StubOutcome, ...] = ()
    prompt_regex: re.Pattern[str] | None = None

    def outcome_for(self, n: int) -> StubOutcome:
        """Outcome for the ``n``-th call *of this entry* (1-based): sequence item over the base.

        The last sequence item repeats once the sequence is exhausted. ``n`` counts every call
        that resolved to this entry (all paths a glob matches), not calls per step path.
        """
        if not self.sequence:
            return self.outcome
        item = self.sequence[min(max(n, 1), len(self.sequence)) - 1]
        return item.merged_over(self.outcome)

    def matches_path(self, step_path: str) -> bool:
        if self.key == step_path:
            return True
        try:
            return StepPath.parse(step_path).matches(self.key)
        except ValueError:
            return fnmatchcase(step_path, self.key)


@dataclass(frozen=True, slots=True)
class StubDefaults:
    """Script-wide defaults (``defaults:``)."""

    latency_ms: float = 0.0
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class StubScript:
    """Parsed stub script; see the module docstring for the YAML shape."""

    steps: tuple[StubEntry, ...] = ()
    match: tuple[StubEntry, ...] = ()
    defaults: StubDefaults = field(default_factory=StubDefaults)

    # -- construction ---------------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: str | Path) -> StubScript:
        """Load a YAML script file."""
        p = Path(path)
        return cls.from_yaml(p.read_text(encoding="utf-8"), source=str(p))

    @classmethod
    def from_yaml(cls, text: str, *, source: str = "<stubs>") -> StubScript:
        """Parse YAML text (``yaml.safe_load``) into a script."""
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise StubScriptError(f"{source}: invalid YAML: {exc}") from exc
        return cls.from_dict(data, source=source)

    @classmethod
    def from_dict(cls, data: Any, *, source: str = "<stubs>") -> StubScript:
        """Build from a plain mapping (``None``/``{}`` → empty script). Raises StubScriptError."""
        if data is None:
            return cls()
        parser = _Parser(source)
        return parser.script(data)

    # -- resolution -----------------------------------------------------------------------------

    def resolve(self, step_path: str, prompt: str) -> StubEntry | None:
        """Exact step key → glob key (declaration order) → first matching ``match`` regex."""
        for entry in self.steps:
            if entry.key == step_path:
                return entry
        for entry in self.steps:
            if entry.matches_path(step_path):
                return entry
        for entry in self.match:
            if entry.prompt_regex is not None and entry.prompt_regex.search(prompt):
                return entry
        return None


class _Parser:
    """Strict dict → script model conversion with source-qualified error messages."""

    def __init__(self, source: str) -> None:
        self.source = source

    def fail(self, where: str, message: str) -> StubScriptError:
        return StubScriptError(f"{self.source}: {where}: {message}")

    def mapping(self, value: Any, where: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise self.fail(where, f"expected a mapping, got {type(value).__name__}")
        return value

    def check_keys(self, data: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
        unknown = sorted(str(k) for k in data if k not in allowed)
        if unknown:
            raise self.fail(
                where, f"unknown key(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}"
            )

    def script(self, data: Any) -> StubScript:
        top = self.mapping(data, "script")
        self.check_keys(top, frozenset({"steps", "match", "defaults"}), "script")
        steps_raw = top.get("steps") or {}
        steps = tuple(
            self.entry(str(key), value, f"steps.{key}")
            for key, value in self.mapping(steps_raw, "steps").items()
        )
        match_raw = top.get("match") or []
        if not isinstance(match_raw, Sequence) or isinstance(match_raw, str | bytes):
            raise self.fail("match", "expected a list of {prompt_regex: ..., ...} mappings")
        match = tuple(self.match_entry(item, f"match[{i}]") for i, item in enumerate(match_raw))
        defaults = self.defaults(top.get("defaults") or {})
        return StubScript(steps=steps, match=match, defaults=defaults)

    def defaults(self, data: Any) -> StubDefaults:
        d = self.mapping(data, "defaults")
        self.check_keys(d, frozenset({"latency_ms", "usage"}), "defaults")
        return StubDefaults(
            latency_ms=self.number(d.get("latency_ms", 0.0), "defaults.latency_ms"),
            usage=self.usage(d["usage"], "defaults.usage") if d.get("usage") is not None else None,
        )

    def entry(self, key: str, data: Any, where: str) -> StubEntry:
        d = self.mapping(data, where)
        self.check_keys(d, _ENTRY_KEYS, where)
        return StubEntry(
            key=key,
            outcome=self.outcome(d, where),
            sequence=self.sequence(d.get("sequence"), where),
        )

    def match_entry(self, data: Any, where: str) -> StubEntry:
        d = self.mapping(data, where)
        self.check_keys(d, _MATCH_KEYS, where)
        pattern = d.get("prompt_regex")
        if not isinstance(pattern, str) or not pattern:
            raise self.fail(where, "needs a non-empty 'prompt_regex' string")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise self.fail(where, f"invalid prompt_regex {pattern!r}: {exc}") from exc
        return StubEntry(
            key=pattern,
            outcome=self.outcome(d, where),
            sequence=self.sequence(d.get("sequence"), where),
            prompt_regex=compiled,
        )

    def sequence(self, data: Any, where: str) -> tuple[StubOutcome, ...]:
        if data is None:
            return ()
        if not isinstance(data, Sequence) or isinstance(data, str | bytes):
            raise self.fail(f"{where}.sequence", "expected a list")
        items: list[StubOutcome] = []
        for i, item in enumerate(data):
            sub_where = f"{where}.sequence[{i}]"
            if isinstance(item, str):
                items.append(StubOutcome(text=item))
            elif isinstance(item, Mapping):
                self.check_keys(item, _OUTCOME_KEYS, sub_where)
                items.append(self.outcome(item, sub_where))
            else:
                raise self.fail(sub_where, "expected a string or a mapping")
        if not items:
            raise self.fail(f"{where}.sequence", "must not be empty")
        return tuple(items)

    def outcome(self, d: Mapping[str, Any], where: str) -> StubOutcome:
        text = d.get("text")
        if text is not None and not isinstance(text, str):
            raise self.fail(f"{where}.text", "expected a string")
        status = d.get("status")
        if status is not None and status not in _RESULT_STATUSES:
            raise self.fail(
                f"{where}.status", f"unknown status {status!r}; one of {sorted(_RESULT_STATUSES)}"
            )
        latency = d.get("latency_ms")
        if "output" in d:
            self.check_json(d["output"], f"{where}.output")
        return StubOutcome(
            text=text,
            output=d.get("output"),
            has_output="output" in d,
            fail=self.failure(d["fail"], f"{where}.fail") if d.get("fail") is not None else None,
            events=self.events(d.get("events"), f"{where}.events"),
            usage=self.usage(d["usage"], f"{where}.usage") if d.get("usage") is not None else None,
            latency_ms=self.number(latency, f"{where}.latency_ms") if latency is not None else None,
            status=status,
            expect=(
                self.expect(d["expect"], f"{where}.expect") if d.get("expect") is not None else None
            ),
        )

    def expect(self, data: Any, where: str) -> StubExpect:
        d = self.mapping(data, where)
        self.check_keys(d, _EXPECT_KEYS, where)
        pattern = d.get("prompt_regex")
        compiled: re.Pattern[str] | None = None
        if pattern is not None:
            if not isinstance(pattern, str) or not pattern:
                raise self.fail(f"{where}.prompt_regex", "expected a non-empty string")
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise self.fail(
                    f"{where}.prompt_regex", f"invalid regex {pattern!r}: {exc}"
                ) from exc
        access = d.get("access")
        if access is not None and access not in _ACCESS_LEVELS:
            raise self.fail(
                f"{where}.access", f"unknown access {access!r}; one of {sorted(_ACCESS_LEVELS)}"
            )
        session = d.get("session")
        if session is not None and session not in _SESSION_EXPECTATIONS:
            raise self.fail(
                f"{where}.session",
                f"expected 'resumed' or 'fresh', got {session!r}",
            )
        model = d.get("model")
        if model is not None and not isinstance(model, str):
            raise self.fail(f"{where}.model", "expected a string")
        schema = d.get("output_schema")
        if schema is not None and not isinstance(schema, bool):
            raise self.fail(f"{where}.output_schema", "expected true or false")
        return StubExpect(
            prompt_regex=compiled,
            prompt_contains=self.strings(d.get("prompt_contains"), f"{where}.prompt_contains"),
            not_contains=self.strings(d.get("not_contains"), f"{where}.not_contains"),
            access=access,
            model=model,
            output_schema=schema,
            session=session,
        )

    def strings(self, data: Any, where: str) -> tuple[str, ...]:
        """One string or a list of strings (``prompt_contains: x`` == ``prompt_contains: [x]``)."""
        if data is None:
            return ()
        if isinstance(data, str):
            return (data,)
        if isinstance(data, Sequence) and not isinstance(data, bytes):
            items = list(data)
            if any(not isinstance(item, str) for item in items):
                raise self.fail(where, "expected a string or a list of strings")
            return tuple(items)
        raise self.fail(where, "expected a string or a list of strings")

    def failure(self, data: Any, where: str) -> StubFailure:
        if isinstance(data, str):
            return StubFailure(message=data)
        d = self.mapping(data, where)
        self.check_keys(d, _FAIL_KEYS, where)
        kind = d.get("kind", "api")
        if kind not in _ERROR_KINDS:
            raise self.fail(
                f"{where}.kind", f"unknown kind {kind!r}; one of {sorted(_ERROR_KINDS)}"
            )
        times = d.get("times")
        if times is not None and (
            isinstance(times, bool) or not isinstance(times, int) or times < 0
        ):
            raise self.fail(f"{where}.times", "expected a non-negative integer")
        return StubFailure(
            kind=kind,
            message=str(d.get("message", "stub failure")),
            transient=bool(d.get("transient", False)),
            times=times,
            raise_error=bool(d.get("raise", False)),
        )

    def usage(self, data: Any, where: str) -> Usage:
        d = self.mapping(data, where)
        self.check_keys(d, _USAGE_KEYS, where)
        values: dict[str, int] = {}
        for key, raw in d.items():
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise self.fail(f"{where}.{key}", "expected a non-negative integer")
            values[str(key)] = raw
        return Usage(**values)

    def check_json(self, value: Any, where: str) -> None:
        """Reject values that are valid YAML but not JSON (dates, timestamps, binary…)."""
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise self.fail(where, f"not JSON-serialisable: {exc}") from exc

    def number(self, value: Any, where: str) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise self.fail(where, "expected a non-negative number")
        return float(value)

    def events(self, data: Any, where: str) -> tuple[AgentEvent, ...]:
        if data is None:
            return ()
        if not isinstance(data, Sequence) or isinstance(data, str | bytes):
            raise self.fail(where, "expected a list of events")
        return tuple(self.event(item, f"{where}[{i}]") for i, item in enumerate(data))

    def event(self, data: Any, where: str) -> AgentEvent:
        d = self.mapping(data, where)
        if "kind" in d:
            kind = d["kind"]
            payload: Mapping[str, Any] = {k: v for k, v in d.items() if k != "kind"}
        elif len(d) == 1:
            ((kind, payload_raw),) = d.items()
            payload = payload_raw if isinstance(payload_raw, Mapping) else {"text": payload_raw}
        else:
            raise self.fail(where, "expected {<kind>: ...} or {kind: <kind>, ...}")
        if kind not in _EVENT_KINDS:
            raise self.fail(where, f"unknown event kind {kind!r}; one of {sorted(_EVENT_KINDS)}")
        event_kind = cast("EventKind", kind)
        known = {k: v for k, v in payload.items() if k in _EVENT_FIELDS}
        extra = {k: v for k, v in payload.items() if k not in _EVENT_FIELDS}
        text = known.get("text")
        if text is None and kind in {"tool_result", "command_output"} and "output" in extra:
            text = extra.pop("output")
        data_field = dict(known.get("data") or {})
        data_field.update(extra)
        self.check_json(data_field, f"{where}.data")
        return AgentEvent(
            kind=event_kind,
            text="" if text is None else str(text),
            name=None if known.get("name") is None else str(known["name"]),
            call_id=None if known.get("call_id") is None else str(known["call_id"]),
            data=data_field,
            nested=bool(known.get("nested", False)),
        )


# --------------------------------------------------------------------------------------------------
# Recorder: a stored run -> a stub script
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One agent call as a finished run remembers it.

    Deliberately store-agnostic: the CLI reads ``run.json`` and the output files and hands the
    recorder plain values, so ``providers/`` never imports ``store/``.

    ``sequential`` says whether the iterations of this path ran in a guaranteed order (a
    ``loop:`` body). Parallel ``each`` items do not, so they keep their own indexed keys instead
    of collapsing into an order-dependent ``sequence:``.
    """

    step_path: str
    text: str | None = None
    output: Any = None
    has_output: bool = False
    usage: Usage | None = None
    failure: StubFailure | None = None
    sequential: bool = True

    def to_outcome(self) -> dict[str, Any]:
        """This call as a stub outcome mapping (the value of a ``steps:`` entry)."""
        data: dict[str, Any] = {}
        if self.has_output:
            data["output"] = self.output
        elif self.text is not None:
            data["text"] = self.text
        if self.failure is not None:
            fail: dict[str, Any] = {"kind": self.failure.kind, "message": self.failure.message}
            if self.failure.transient:
                fail["transient"] = True
            if self.failure.times is not None:
                fail["times"] = self.failure.times
            data["fail"] = fail
        if self.usage is not None and self.usage.total:
            data["usage"] = {
                key: value
                for key, value in (
                    ("input", self.usage.input),
                    ("cached_input", self.usage.cached_input),
                    ("cache_write", self.usage.cache_write),
                    ("output", self.usage.output),
                    ("reasoning", self.usage.reasoning),
                )
                if value
            }
        return data


_INDEX_RE = re.compile(r"\[[^\]]*\]")


def _unindexed(step_path: str) -> str:
    """``build[2]/implement`` / ``build[*]/implement`` → ``build/implement`` — the shape a
    workflow declares, so a script key can be compared with the static step paths."""
    return _INDEX_RE.sub("", step_path)


def entry_expects(entry: StubEntry) -> bool:
    """Whether ``entry`` asserts anything (an ``expect:`` block on the entry or on a sequence
    item) — the subset of stale keys whose silence is actively harmful."""
    return entry.outcome.expect is not None or any(
        item.expect is not None for item in entry.sequence
    )


def unmatched_expect_keys(script: StubScript, known_paths: Iterable[str]) -> tuple[str, ...]:
    """``steps:`` keys carrying an ``expect:`` block that none of ``known_paths`` can resolve.

    ``known_paths`` are the workflow's declared prompt-step paths (``build/implement``); loop and
    ``each`` indices are ignored on both sides, so ``build[*]/implement`` and ``build[2]/implement``
    both match ``build/implement``. An assertion written against a renamed or typo'd step never
    fires, and a suite that asserts nothing stays green — so the caller refuses the run instead.
    """
    bare = [_unindexed(path) for path in known_paths]
    stale: list[str] = []
    for entry in script.steps:
        if not entry_expects(entry):
            continue
        key = _unindexed(entry.key)
        if not any(path == key or fnmatchcase(path, key) for path in bare):
            stale.append(entry.key)
    return tuple(stale)


def glob_key(step_path: str) -> str:
    """``build[2]/implement`` → ``build[*]/implement`` — the run-time key shape a stub script uses
    for every iteration of one body step."""
    return re.sub(r"\[\d+\]", "[*]", step_path)


def _index_key(step_path: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\[(\d+)\]", step_path))


def record_script(
    calls: Sequence[RecordedCall], *, defaults: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Turn recorded calls into a stub-script mapping (``StubScript.from_dict`` accepts it).

    Calls that share a glob key are one entry. Identical answers stay one outcome; differing
    answers of an *ordered* body become a ``sequence:`` under the glob (the n-th iteration gets
    the n-th item, and the last one repeats if the loop runs longer next time), while differing
    answers of a parallel ``each`` body are written as their own indexed keys — replaying those
    through a sequence would hand answers to whichever item happened to call first.

    A group in which any call failed *transiently* also keeps its indexed keys: a ``sequence:``
    advances per CALL, and the engine retries a transient failure (``DEFAULT_PROMPT_RETRY``), so
    the retry would eat the next iteration's answer and shift the whole loop by one.
    """
    groups: dict[str, list[RecordedCall]] = {}
    for call in calls:
        groups.setdefault(glob_key(call.step_path), []).append(call)
    steps: dict[str, Any] = {}
    for key, group in groups.items():
        ordered = sorted(group, key=lambda call: _index_key(call.step_path))
        outcomes = [call.to_outcome() for call in ordered]
        if all(outcome == outcomes[0] for outcome in outcomes):
            # one call, or every iteration answered the same: one entry under the glob key
            # (a retry resolves to the same entry and gets the same answer)
            steps[key] = outcomes[0]
        elif all(call.sequential for call in ordered) and not any(
            _is_retried(call) for call in ordered
        ):
            steps[key] = {"sequence": [_sequence_item(outcome) for outcome in outcomes]}
        else:
            for call, outcome in zip(ordered, outcomes, strict=True):
                steps[call.step_path] = outcome
    script: dict[str, Any] = {"steps": steps}
    script["defaults"] = dict(defaults) if defaults is not None else {"latency_ms": 0}
    return script


def _is_retried(call: RecordedCall) -> bool:
    """Whether replaying this call costs more than one call: a transient failure the engine will
    retry (each attempt resolves the script again, advancing a ``sequence:`` by one)."""
    return call.failure is not None and call.failure.transient


def _sequence_item(outcome: Mapping[str, Any]) -> Any:
    """A text-only outcome is written as a bare string (the script parser accepts both)."""
    if set(outcome) == {"text"}:
        return outcome["text"]
    return dict(outcome)


# --------------------------------------------------------------------------------------------------
# Default output from a JSON schema
# --------------------------------------------------------------------------------------------------


def minimal_instance(schema: Mapping[str, Any] | None) -> Any:
    """Smallest value satisfying a (simple) JSON schema: required fields with type defaults.

    ``default``/``const``/first ``enum`` value win; ``null`` in a type union yields ``None``;
    ``object`` recurses into ``required`` properties; local ``$ref`` pointers (``#/$defs/…``,
    ``#/definitions/…``, any ``#/`` JSON pointer into the root schema) are followed with a
    recursion guard (a ref already on the current path yields ``{}``); ``minLength``,
    ``minItems``, ``minimum``/``exclusiveMinimum`` and ``maximum``/``exclusiveMaximum`` are
    honoured. Anything else (remote refs, ``pattern``, ``uniqueItems``, ``multipleOf``, ``not``…)
    is ignored and may produce an instance that fails validation; the stub never raises here.
    """
    if not isinstance(schema, Mapping):
        return {}
    return _minimal(schema, schema, frozenset())


def _resolve_pointer(root: Mapping[str, Any], ref: str) -> Any:
    """Walk a local JSON pointer (``#/a/b``) into ``root``; ``None`` when it does not resolve."""
    if not ref.startswith("#/"):
        return None
    node: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, Sequence) and not isinstance(node, str | bytes) and part.isdigit():
            index = int(part)
            if index >= len(node):
                return None
            node = node[index]
        else:
            return None
    return node


def _bounded_number(schema: Mapping[str, Any], *, integer: bool) -> int | float:
    """``0`` clamped into ``[minimum, maximum]`` (exclusive bounds nudged by one)."""
    lo: int | float | None = None
    hi: int | float | None = None
    if isinstance(schema.get("minimum"), int | float) and not isinstance(schema["minimum"], bool):
        lo = schema["minimum"]
    if isinstance(schema.get("exclusiveMinimum"), int | float) and not isinstance(
        schema["exclusiveMinimum"], bool
    ):
        lo = schema["exclusiveMinimum"] + 1
    if isinstance(schema.get("maximum"), int | float) and not isinstance(schema["maximum"], bool):
        hi = schema["maximum"]
    if isinstance(schema.get("exclusiveMaximum"), int | float) and not isinstance(
        schema["exclusiveMaximum"], bool
    ):
        hi = schema["exclusiveMaximum"] - 1
    value: int | float = 0
    if lo is not None and lo > value:
        value = lo
    elif hi is not None and hi < value:
        value = hi
    if integer:
        return math.ceil(value) if lo is not None and value == lo else math.floor(value)
    return float(value)


def _minimal(schema: Any, root: Mapping[str, Any], active: frozenset[str]) -> Any:
    if not isinstance(schema, Mapping):
        return {}
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in active:
            return {}
        target = _resolve_pointer(root, ref)
        if not isinstance(target, Mapping):
            return {}
        return _minimal(target, root, active | {ref})
    if "const" in schema:
        return schema["const"]
    if "default" in schema:
        return schema["default"]
    enum = schema.get("enum")
    if isinstance(enum, Sequence) and not isinstance(enum, str | bytes) and len(enum) > 0:
        return enum[0]
    for combinator in ("oneOf", "anyOf", "allOf"):
        options = schema.get(combinator)
        if isinstance(options, Sequence) and not isinstance(options, str | bytes) and options:
            return _minimal(options[0], root, active)
    kind = schema.get("type")
    if isinstance(kind, Sequence) and not isinstance(kind, str):
        kind = "null" if "null" in kind else (kind[0] if kind else None)
    if kind is None and "properties" in schema:
        kind = "object"
    if kind == "null":
        return None
    if kind == "string":
        min_len = schema.get("minLength")
        n = min_len if isinstance(min_len, int) and not isinstance(min_len, bool) else 0
        return "x" * max(n, 0)
    if kind == "integer":
        return _bounded_number(schema, integer=True)
    if kind == "number":
        return _bounded_number(schema, integer=False)
    if kind == "boolean":
        return False
    if kind == "array":
        min_items = schema.get("minItems")
        n = min_items if isinstance(min_items, int) and not isinstance(min_items, bool) else 0
        if n <= 0:
            return []
        items_schema = schema.get("items")
        if isinstance(items_schema, Sequence) and not isinstance(items_schema, str | bytes):
            items_schema = items_schema[0] if items_schema else None
        return [_minimal(items_schema, root, active) for _ in range(n)]
    props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    required = schema.get("required") or []
    return {
        str(name): _minimal(props.get(name) if isinstance(props, Mapping) else None, root, active)
        for name in required
    }


# --------------------------------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------------------------------


def _word_chunks(text: str) -> list[str]:
    return [m.group(0) for m in _WORD_RE.finditer(text)]


class StubProvider:
    """Scripted provider. ``settings`` keys: ``script`` (dict | StubScript), ``script_path``."""

    id: str = "stub"
    capabilities: ProviderCapabilities = STUB_CAPABILITIES

    def __init__(
        self,
        settings: Mapping[str, Any] | None = None,
        *,
        script: StubScript | Mapping[str, Any] | None = None,
    ) -> None:
        settings = dict(settings or {})
        if script is None:
            script = settings.get("script")
        if script is None and settings.get("script_path"):
            script = StubScript.from_file(str(settings["script_path"]))
        if script is None:
            self.script = StubScript()
        elif isinstance(script, StubScript):
            self.script = script
        else:
            self.script = StubScript.from_dict(script)
        self.settings: Mapping[str, Any] = settings
        self.calls: list[AgentRequest] = []
        self.run_id: str = ""
        self.workdir: str = ""
        self.env: Mapping[str, str] = {}
        self.max_parallel: int = 0
        self.closed: bool = False
        self._path_counts: dict[str, int] = {}
        self._entry_counts: dict[int, int] = {}

    # -- Provider protocol ----------------------------------------------------------------------

    async def open(
        self, *, run_id: str, workdir: str, env: Mapping[str, str], max_parallel: int
    ) -> None:
        """Record per-run parameters (no resources to acquire)."""
        self.run_id = run_id
        self.workdir = workdir
        self.env = dict(env)
        self.max_parallel = max_parallel
        self.closed = False

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
        """Always healthy: there is no SDK or credential to check (``sdk_version`` is None)."""
        details = (f"scripted provider; no SDK (rayspec {__version__})",) + (
            ("probe: ok",) if probe else ()
        )
        return ProviderHealth(ok=True, sdk_version=None, auth="ok", details=details)

    async def aclose(self) -> None:
        """Nothing to release; marks the provider closed."""
        self.closed = True

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        """Resolve the script for ``req`` and replay it as an event stream + result."""
        started = time.perf_counter()
        self.calls.append(req)
        n = self._path_counts.get(req.step_path, 0) + 1
        self._path_counts[req.step_path] = n
        session_ref = f"stub:{req.step_path}:{n}"
        entry = self.script.resolve(req.step_path, req.prompt)
        if entry is None:
            outcome, entry_call = StubOutcome(), 0
        else:
            entry_call = self._entry_counts.get(id(entry), 0) + 1
            self._entry_counts[id(entry)] = entry_call
            outcome = entry.outcome_for(entry_call)
        raw: dict[str, Any] = {
            "stub": True,
            "matched": entry.key if entry is not None else None,
            "call": n,
            "entry_call": entry_call,
            "resume_session": req.resume_session,
            "fork_session": req.fork_session,
        }

        await emit(AgentEvent(kind="session", text=session_ref, data={"session_ref": session_ref}))

        # assertions outrank everything the SCRIPT can do — a mismatched request must be
        # loud, not answered (and not slept through, and not turned into the scripted failure).
        # The step's own `allow_failure:` still tolerates the resulting failure, like any other.
        if outcome.expect is not None:
            reasons, offset = outcome.expect.check(req)
            if reasons:
                message = outcome.expect.failure_message(req, reasons, offset)
                error = AgentError(kind="stub_expectation", message=message, transient=False)
                await emit(
                    AgentEvent(kind="error", text=message, data={"kind": "stub_expectation"})
                )
                return self._result(
                    status="error",
                    text="",
                    session_ref=session_ref,
                    usage=Usage(),
                    req=req,
                    started=started,
                    raw={**raw, "expectation_failed": list(reasons)},
                    error=error,
                )

        latency_ms = (
            outcome.latency_ms
            if outcome.latency_ms is not None
            else self.script.defaults.latency_ms
        )
        if req.timeout_s is not None and latency_ms / 1000.0 > req.timeout_s:
            # Simulated provider-side timeout. The engine owns the real deadline
            # (anyio.fail_after(timeout_s)), so return well before it fires — otherwise the
            # scripted `timeout` status would never be observable. To exercise engine-level
            # cancellation instead, script a latency and leave req.timeout_s unset.
            await anyio.sleep(req.timeout_s / 2)
            error = AgentError(
                kind="timeout",
                message=f"stub: latency {latency_ms:g}ms exceeds timeout {req.timeout_s:g}s",
                transient=False,
            )
            await emit(AgentEvent(kind="error", text=error.message, data={"kind": "timeout"}))
            return self._result(
                status="timeout",
                text="",
                session_ref=session_ref,
                usage=Usage(),
                req=req,
                started=started,
                raw=raw,
                error=error,
            )
        if latency_ms > 0:
            await anyio.sleep(latency_ms / 1000.0)

        if outcome.fail is not None and outcome.fail.applies(n):
            failure = outcome.fail
            if failure.raise_error:
                raise ProviderError(failure.message, transient=failure.transient, kind=failure.kind)
            error = AgentError(
                kind=failure.kind, message=failure.message, transient=failure.transient
            )
            await emit(
                AgentEvent(
                    kind="error",
                    text=failure.message,
                    data={"kind": failure.kind, "transient": failure.transient},
                )
            )
            return self._result(
                status="error",
                text="",
                session_ref=session_ref,
                usage=Usage(),
                req=req,
                started=started,
                raw=raw,
                error=error,
            )

        text, structured = self._payload(outcome, req)
        # scripted events (tool calls/results, …) first, then the answer — a transcript reads
        # tool call → tool result → answer, like a real agent
        for event in outcome.events:
            await emit(replace(event, data=dict(event.data)))
        for chunk in _word_chunks(text):
            await emit(AgentEvent(kind="text_delta", text=chunk))
        await emit(AgentEvent(kind="text", text=text))

        usage = outcome.usage or self.script.defaults.usage
        if usage is None:
            usage = Usage(input=len(req.prompt) // 4, output=len(text) // 4)
        status: ResultStatus = outcome.status or "success"
        return self._result(
            status=status,
            text=text,
            structured=structured,
            session_ref=session_ref,
            usage=usage,
            req=req,
            started=started,
            raw=raw,
        )

    # -- helpers --------------------------------------------------------------------------------

    @staticmethod
    def _payload(outcome: StubOutcome, req: AgentRequest) -> tuple[str, Any]:
        if outcome.has_output:
            structured = outcome.output
            text = outcome.text if outcome.text is not None else json.dumps(structured, indent=2)
            return text, structured
        if outcome.text is not None:
            return outcome.text, None
        if req.output_schema is not None:
            structured = minimal_instance(req.output_schema)
            return json.dumps(structured, indent=2), structured
        return "[stub] " + req.prompt[:80], None

    @staticmethod
    def _result(
        *,
        status: ResultStatus,
        text: str,
        session_ref: str,
        usage: Usage,
        req: AgentRequest,
        started: float,
        raw: Mapping[str, Any],
        structured: Any = None,
        error: AgentError | None = None,
    ) -> AgentResult:
        return AgentResult(
            status=status,
            text=text,
            structured=structured,
            session_ref=session_ref,
            usage=usage,
            cost_usd=None,
            cost_source="none",
            duration_ms=int((time.perf_counter() - started) * 1000),
            num_turns=1,
            model=req.model or "stub",
            error=error,
            raw=raw,
        )


__all__ = [
    "EXCERPT_LIMIT",
    "RecordedCall",
    "StubDefaults",
    "StubEntry",
    "StubExpect",
    "StubFailure",
    "StubOutcome",
    "StubProvider",
    "StubScript",
    "StubScriptError",
    "entry_expects",
    "glob_key",
    "minimal_instance",
    "prompt_excerpt",
    "record_script",
    "unmatched_expect_keys",
]
