# SPDX-License-Identifier: Apache-2.0
"""One :class:`Redactor` for every writer.

Module boundary: pure text transformation. This module knows the *values* it must never let
through and nothing about runs, stores, sinks or configuration; the callers wire it in:

* :class:`~rayspec.store.file.FileRunStore` redacts every byte it writes (``run.json``,
  ``events.jsonl``, ``stream.jsonl``, output files) — a writer that goes through the store is
  covered automatically. Anything JSON-shaped goes through :meth:`Redactor.redact_dump` /
  :meth:`Redactor.redact_obj`, which work on the PARSED value: replacing a secret in the
  serialised text turns a bare JSON token (a numeric secret) into an unquoted marker and leaves
  a file that no longer parses;
* the shared subprocess runner redacts ``stdout.log``/``stderr.log`` and the stdout/stderr
  stream records as they are produced;
* :class:`RedactingSink` wraps the event sinks so the console and ``--json`` are covered too.

**Honest limits.** Redaction is EXACT MATCH and BEST EFFORT. It replaces byte-for-byte
occurrences of values rayspec knows (declared ``secret: true`` inputs and every ``secrets:``
entry) plus, when the user opts in, a handful of well-known credential *shapes*. It cannot
catch a value an agent or a script transformed — base64, a URL-encoded copy, "the token starts
with ghp_ and ends with 3f", a value reassembled from pieces. The load-time refusals
(``secret: true`` values never reach a prompt, an expression or an output) remain the guarantee;
this is the safety net under them, not a replacement.

Values shorter than :data:`MIN_REDACTABLE_LEN` are deliberately NOT redacted: replacing every
``ab`` in a transcript destroys the log without protecting anything. :attr:`Redactor.skipped`
names them so a caller can warn.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rayspec.events.model import RunEvent, StreamRecord

_log = logging.getLogger(__name__)

#: The replacement text; ``name`` is the secret's name or the detector that matched.
REDACTION = "[REDACTED:{name}]"
#: Values shorter than this are never redacted (they would match everywhere).
MIN_REDACTABLE_LEN = 4
#: Longest body a ``pem`` block may have and still be caught across a chunk boundary.
PEM_MAX_BODY = 8192
#: Upper bound on what a :class:`StreamRedactor` holds back for a detector, per detector; the
#: name is kept because it is the documented worst case of the short-token detectors.
DETECTOR_HOLD = 512
#: How often :meth:`Redactor.redact_dump` may undo a substitution a model cannot hold. Every
#: round puts back everything the validator complained about, so one is nearly always enough.
_MAX_UNDO_ROUNDS = 8

#: Opt-in builtin detectors (``config.redact.detectors``) — off by default on purpose: a false
#: positive in a run log is worse than the gap. Every pattern is BOUNDED, because the bound is
#: also how much a :class:`StreamRedactor` must hold back to catch the shape across a chunk
#: boundary (an unbounded ``pem`` body could never be caught with a fixed hold).
_DETECTORS: dict[str, str] = {
    "github": r"gh[pousr]_[A-Za-z0-9]{16,4096}",
    "openai": r"sk-(?:proj-)?[A-Za-z0-9_-]{16,4096}",
    "aws": r"(?:AKIA|ASIA)[0-9A-Z]{16}",
    "jwt": r"eyJ[A-Za-z0-9_-]{6,4096}\.[A-Za-z0-9_-]{6,4096}\.[A-Za-z0-9_-]{6,4096}",
    "pem": (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
        rf"[\s\S]{{0,{PEM_MAX_BODY}}}?"
        r"-----END [A-Z ]*PRIVATE KEY-----"
    ),
}

#: Per detector: ``(anchor, the character class its match continues with, characters after the
#: anchor)``. A :class:`StreamRedactor` holds text back from the last position where a *prefix*
#: of the anchor (or the anchor plus its continuation) reaches the end of the buffer — so a
#: shape that is still being written is completed by the next chunk instead of being missed,
#: and ordinary text is passed through with no delay at all.
_DETECTOR_OPEN: dict[str, tuple[str, str, int]] = {
    "github": ("gh", r"[A-Za-z0-9_]", 4100),
    "openai": ("sk", r"[A-Za-z0-9_-]", 4100),
    "aws": ("A", r"[0-9A-Z]", 20),
    "jwt": ("eyJ", r"[A-Za-z0-9_.-]", 12300),
    "pem": ("-----BEGIN ", r"[\s\S]", PEM_MAX_BODY + 64),
}


def detector_patterns(names: Iterable[str]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """``((name, compiled), …)`` for the known detector names; unknown names are ignored."""
    out: list[tuple[str, re.Pattern[str]]] = []
    for name in dict.fromkeys(names):
        pattern = _DETECTORS.get(name)
        if pattern is not None:
            out.append((name, re.compile(pattern)))
    return tuple(out)


@cache
def _open_pattern(name: str) -> tuple[re.Pattern[str], int]:
    """``(pattern, cap)`` matching a tail of a buffer that could still grow into ``name``.

    The alternation is the anchor followed by up to ``cap`` continuation characters, or any
    proper prefix of the anchor — each anchored at the END of the string, because only a tail
    can still be completed by the next chunk.
    """
    anchor, tail, cap = _DETECTOR_OPEN[name]
    alternatives = [re.escape(anchor) + f"{tail}{{0,{cap}}}"]
    alternatives += [re.escape(anchor[:i]) for i in range(len(anchor) - 1, 0, -1)]
    return re.compile("(?:" + "|".join(alternatives) + r")\Z"), cap + len(anchor)


def _suffix_prefix_len(text: str, needle: str) -> int:
    """Length of the longest suffix of ``text`` that is a PROPER prefix of ``needle`` (0: none).

    That suffix is exactly what a stream must hold back for ``needle``: anything shorter would
    let a value straddling the chunk boundary through, anything longer only adds latency.
    """
    span = min(len(needle) - 1, len(text))
    if span <= 0:
        return 0
    tail = text[-span:]
    start = 0
    while True:
        index = tail.find(needle[0], start)
        if index < 0:
            return 0
        if needle.startswith(tail[index:]):
            return len(tail) - index
        start = index + 1


def _variants(value: str) -> tuple[str, ...]:
    """The literal plus the forms a writer may serialise it into.

    ``run.json``/``events.jsonl``/``stream.jsonl`` are redacted as *serialised JSON text*, so a
    value containing a quote, a backslash or a newline only matches in its escaped form.
    """
    escaped = json.dumps(value)[1:-1]
    return (value,) if escaped == value else (value, escaped)


@dataclass(frozen=True, slots=True)
class Redactor:
    """Replaces known secret values (and, opt-in, known credential shapes) with a marker.

    Build one with :meth:`build`; :data:`NULL_REDACTOR` is the shared no-op. ``bool(redactor)``
    is ``False`` when it would not change any text, which every caller uses to skip the work.
    """

    #: ``(needle, replacement)`` pairs, longest needle first (so an overlapping short secret
    #: cannot mask a longer one).
    literals: tuple[tuple[str, str], ...] = ()
    #: ``(name, compiled pattern)`` for the enabled builtin detectors.
    patterns: tuple[tuple[str, re.Pattern[str]], ...] = ()
    #: Names whose value was too short to redact (see :data:`MIN_REDACTABLE_LEN`).
    skipped: tuple[str, ...] = ()
    #: Length of the longest literal — how much a :class:`StreamRedactor` must hold back.
    max_len: int = field(default=0)

    @classmethod
    def build(cls, secrets: Mapping[str, Any], *, detectors: Sequence[str] = ()) -> Redactor:
        """A redactor for ``{name: value}`` plus the named builtin detectors.

        Non-string values are stringified (an integer secret input is still a secret); ``None``
        and values shorter than :data:`MIN_REDACTABLE_LEN` are skipped.
        """
        literals: list[tuple[str, str]] = []
        skipped: list[str] = []
        seen: set[str] = set()
        for name, raw in secrets.items():
            if raw is None:
                continue
            value = raw if isinstance(raw, str) else str(raw)
            if len(value) < MIN_REDACTABLE_LEN:
                skipped.append(name)
                continue
            for variant in _variants(value):
                if variant not in seen:
                    seen.add(variant)
                    literals.append((variant, REDACTION.format(name=name)))
        literals.sort(key=lambda pair: len(pair[0]), reverse=True)
        return cls(
            literals=tuple(literals),
            patterns=detector_patterns(detectors),
            skipped=tuple(skipped),
            max_len=max((len(n) for n, _ in literals), default=0),
        )

    def __bool__(self) -> bool:
        return bool(self.literals or self.patterns)

    @property
    def hold(self) -> int:
        """UPPER BOUND on what a :class:`StreamRedactor` may hold back.

        Not what it *does* hold back: :meth:`StreamRedactor.feed` holds back only the tail that
        could still grow into a match (usually nothing), so a stream that carries no secret is
        never delayed. This bound is what the documentation quotes as the worst case.
        """
        if not self:
            return 0
        detectors = max(
            (_open_pattern(name)[1] for name, _ in self.patterns if name in _DETECTOR_OPEN),
            default=0,
        )
        return max(self.max_len - 1, detectors)

    def redact(self, text: str) -> str:
        """``text`` with every known value (and enabled shape) replaced."""
        if not text or not self:
            return text
        for needle, replacement in self.literals:
            if needle in text:
                text = text.replace(needle, replacement)
        for name, pattern in self.patterns:
            text = pattern.sub(REDACTION.format(name=name), text)
        return text

    def redact_obj(self, value: Any, *, numbers: bool = True) -> Any:
        """:meth:`redact` applied to every string inside a JSON-shaped value.

        A *number* that IS a secret (a numeric account id, a PIN, a numeric token) becomes the
        marker string: redacting the serialised text instead would replace a bare JSON token
        with ``[REDACTED:…]`` and leave an invalid document behind. Only a number whose whole
        text equals a secret is replaced — rewriting the digits *inside* a longer number would
        produce the same broken document.

        ``numbers=False`` leaves every number alone. It is for the caller that has just
        discovered a substitution landed in a position where a string is not admissible (see
        :meth:`redact_dump`): there the match is a coincidence rather than a leak.
        """
        if not self:
            return value
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {k: self.redact_obj(v, numbers=numbers) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.redact_obj(v, numbers=numbers) for v in value]
        if isinstance(value, bool) or value is None:
            return value  # `true`/`null` are too common to match a value against
        if numbers and isinstance(value, int | float):
            text = str(value)
            for needle, replacement in self.literals:
                if needle == text:
                    return replacement
        return value

    def redact_dump(self, model: BaseModel) -> Any:
        """``model`` as a JSON-able mapping with every secret replaced in the PARSED values.

        This is what a writer must persist a record through. Redacting the *serialised* text
        instead corrupts the document whenever a secret is a bare JSON token: a numeric secret
        rewrites ``"budget": 4242`` to an unquoted ``"budget": [REDACTED:pin]``, and the file
        no longer parses — a checkpoint that cannot be read back is worse than the leak it was
        trying to prevent.

        A substitution that would make the dump invalid *for its model* is undone at exactly the
        field it broke, and nowhere else: a structural number (a duration, a token count, an exit
        code) that happens to equal a secret is a coincidence rather than a leak, and it cannot
        hold a marker string. Every free-form value keeps its redaction. If the dump is still
        invalid after that, the redacted form is written anyway and the caller is warned — a
        persisted secret is the worse outcome.
        """
        data = model.model_dump(mode="json")
        if not self:
            return data
        out = self.redact_obj(data)
        cls = type(model)
        for _ in range(_MAX_UNDO_ROUNDS):
            error = _validation_error(cls, out)
            if error is None:
                return out
            if not _restore_originals(out, data, error):
                break
        _log.warning(
            "redaction leaves this %s unreadable; writing it redacted anyway, because a "
            "persisted secret is worse than an unreadable record",
            cls.__name__,
        )
        return out

    def stream(self) -> StreamRedactor:
        """A fresh :class:`StreamRedactor` over this redactor."""
        return StreamRedactor(self)


def _validation_error(cls: type[BaseModel], data: Any) -> ValidationError | None:
    """The error ``data`` fails ``cls`` with, ``None`` when it parses back (:meth:`redact_dump`)."""
    try:
        cls.model_validate(data)
    except ValidationError as exc:
        return exc
    return None


def _member(container: Any, key: Any) -> bool:
    """True when ``key`` addresses an element of the mapping/sequence ``container``."""
    if isinstance(container, dict):
        return key in container
    if isinstance(container, list):
        return isinstance(key, int) and -len(container) <= key < len(container)
    return False


def _restore_originals(out: Any, data: Any, error: ValidationError) -> bool:
    """Put the original value back at every location ``error`` complains about.

    Returns True when at least one location changed, so :meth:`Redactor.redact_dump` knows
    whether another round can make progress. A location pydantic reports below the leaf (the
    ``"int"`` tail of a union) is resolved to the deepest element that exists in both dumps.
    """
    changed = False
    for entry in error.errors():
        target: tuple[Any, Any, Any] | None = None
        cursor_out, cursor_data = out, data
        for part in entry["loc"]:
            if not (_member(cursor_out, part) and _member(cursor_data, part)):
                break
            target = (cursor_out, cursor_data, part)
            cursor_out, cursor_data = cursor_out[part], cursor_data[part]
        if target is None:
            continue
        container_out, container_data, key = target
        if container_out[key] != container_data[key]:
            container_out[key] = container_data[key]
            changed = True
    return changed


#: The shared no-op redactor (a store or sink without secrets uses this).
NULL_REDACTOR = Redactor()


class StreamRedactor:
    """Chunk-boundary-safe redaction of ONE text stream.

    A secret split across two ``text_delta`` chunks (``ghp_SEC`` + ``RET…``) matches neither
    chunk on its own, so :meth:`feed` holds back the tail that could still *grow* into a match —
    the longest suffix that is a proper prefix of a known value, or that a detector's shape is
    still in the middle of. Text that cannot be part of any secret is returned immediately, so a
    live log or console tree never lags behind a long-running step. The concatenation of
    everything ``feed``/``flush`` return equals the redaction of the concatenation of everything
    fed in; only the chunk *boundaries* move.

    A caller MUST :meth:`flush` at the end of the stream (the store does it when the step
    finishes), otherwise a pending partial value is dropped.
    """

    __slots__ = ("_pending", "_redactor")

    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor
        self._pending = ""

    @property
    def redactor(self) -> Redactor:
        """The redactor this stream applies."""
        return self._redactor

    def feed(self, text: str) -> str:
        """The safe prefix of everything fed so far that has not been returned yet."""
        if not self._redactor:
            return text
        if not text:
            return ""
        buffered = self._pending + text
        hold = self._hold(buffered)
        if hold >= len(buffered):
            self._pending = buffered
            return ""
        cut = len(buffered) - hold
        self._pending = buffered[cut:]
        return self._redactor.redact(buffered[:cut])

    def _hold(self, text: str) -> int:
        """How many trailing characters of ``text`` could still become part of a match."""
        hold = 0
        for needle, _ in self._redactor.literals:
            hold = max(hold, _suffix_prefix_len(text, needle))
        for name, _ in self._redactor.patterns:
            if name not in _DETECTOR_OPEN:
                continue
            pattern, cap = _open_pattern(name)
            region = text[-cap:] if len(text) > cap else text
            match = pattern.search(region)
            if match is not None:
                hold = max(hold, len(region) - match.start())
        return min(hold, len(text))

    def flush(self) -> str:
        """The held-back tail (redacted); the stream is empty afterwards."""
        if not self._pending:
            return ""
        out = self._redactor.redact(self._pending)
        self._pending = ""
        return out


class RedactingSink:
    """Wraps one event sink so nothing it observes carries a secret value.

    Sinks print (console) and serialise (``--json``) but never persist, so redacting them is a
    separate concern from the store's. Wrapping is deliberately done ONCE, in the CLI's sink
    factory, rather than inside each sink: there is a single implementation of the rule and a
    sink written later is covered by construction.

    Stream records go through a :class:`StreamRedactor` per ``(step_path, kind, attempt)`` (a
    secret split across two deltas is caught); the held-back tail is emitted when the step
    finishes and on :meth:`aclose`. Attributes the engine looks for on a sink (``suspended``,
    ``pause``, ``resume``) are delegated to the wrapped sink unchanged.
    """

    __slots__ = ("_redactor", "_streams", "inner")

    def __init__(self, inner: Any, redactor: Redactor) -> None:
        self.inner = inner
        self._redactor = redactor
        self._streams: dict[tuple[str, str, int], StreamRedactor] = {}

    def __getattr__(self, name: str) -> Any:
        # ``suspended``/``pause``/``resume`` and anything else the engine probes for
        return getattr(self.inner, name)

    async def emit(self, event: RunEvent) -> None:
        """Forward ``event`` with its ``data`` redacted; flush a finished step's tail first."""
        if self._redactor:
            if event.type.value == "step.finished" and event.step_path:
                await self._flush(event.step_path)
            elif event.type.value == "run.finished":
                await self._flush(None)
            if event.data:
                event = event.model_copy(update={"data": self._redactor.redact_obj(event.data)})
        await self.inner.emit(event)

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        """Forward ``record`` with its text held back across the chunk boundary and redacted."""
        if self._redactor:
            record = self._redact(step_path, record)
        await self.inner.emit_stream(step_path, record)

    async def aclose(self) -> None:
        """Flush every held-back tail, then close the wrapped sink."""
        await self._flush(None)
        await self.inner.aclose()

    def _redact(self, step_path: str, record: StreamRecord) -> StreamRecord:
        update: dict[str, Any] = {}
        if record.text:
            key = (step_path, record.kind, record.attempt)
            stream = self._streams.get(key)
            if stream is None:
                stream = self._streams[key] = StreamRedactor(self._redactor)
            update["text"] = stream.feed(record.text)
        if record.data:
            update["data"] = self._redactor.redact_obj(record.data)
        if record.name:
            update["name"] = self._redactor.redact(record.name)
        return record.model_copy(update=update) if update else record

    async def _flush(self, step_path: str | None) -> None:
        from rayspec.events.model import StreamRecord as _StreamRecord

        for key in [k for k in self._streams if step_path is None or k[0] == step_path]:
            tail = self._streams.pop(key).flush()
            if tail:
                await self.inner.emit_stream(
                    key[0], _StreamRecord(kind=key[1], attempt=key[2], text=tail)
                )


__all__ = [
    "DETECTOR_HOLD",
    "MIN_REDACTABLE_LEN",
    "NULL_REDACTOR",
    "PEM_MAX_BODY",
    "REDACTION",
    "RedactingSink",
    "Redactor",
    "StreamRedactor",
    "detector_patterns",
]
