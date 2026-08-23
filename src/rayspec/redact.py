# SPDX-License-Identifier: Apache-2.0
"""One :class:`Redactor` for every writer.

Module boundary: pure text transformation. This module knows the *values* it must never let
through and nothing about runs, stores, sinks or configuration; the callers wire it in:

* :class:`~rayspec.store.file.FileRunStore` redacts every file it writes (``run.json``,
  ``events.jsonl``, ``stream.jsonl``, output files) — a writer that goes through the store is
  covered automatically. Anything JSON-shaped goes through :meth:`Redactor.redact_dump` /
  :meth:`Redactor.redact_obj`, which work on the PARSED value: replacing a secret in the
  serialised text turns a bare JSON token (a numeric secret) into an unquoted marker and leaves
  a file that no longer parses. Free-form KEYS are redacted beside their values; a record's own
  structure (field names, the step paths its steps are keyed by), the identity fields the writer
  names (``preserve``) and the ones a record declares for itself
  (:data:`IDENTITY_FIELDS_ATTR`) are left alone on purpose — see :meth:`Redactor.redact_dump`;
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
from dataclasses import dataclass, field, replace
from functools import cache
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rayspec.events.model import RunEvent, StreamRecord

_log = logging.getLogger(__name__)

#: The replacement text; ``name`` is the secret's name or the detector that matched.
REDACTION = "[REDACTED:{name}]"
#: The two halves of :data:`REDACTION` around the name, so :func:`_name_in` is its exact inverse
#: and cannot drift if the marker is ever reworded.
_MARKER_PREFIX, _MARKER_SUFFIX = REDACTION.split("{name}", 1)
#: Values shorter than this are never redacted (they would match everywhere).
MIN_REDACTABLE_LEN = 4
#: Longest body a ``pem`` block may have and still be caught across a chunk boundary.
PEM_MAX_BODY = 8192
#: Upper bound on what a :class:`StreamRedactor` holds back for a detector, per detector; the
#: name is kept because it is the documented worst case of the short-token detectors.
DETECTOR_HOLD = 512
#: The ``ClassVar`` a record model sets to name the fields of its OWN that are identity rather
#: than content — the strings something later resolves the record BY. :meth:`Redactor.redact_dump`
#: honours it wherever that model appears, at any depth, so a nested record can protect its
#: address without the top-level writer having to know the shape of everything below it
#: (:class:`rayspec.store.model.StepRecord` declares ``path``, for one).
IDENTITY_FIELDS_ATTR = "redaction_identity"
#: How often :meth:`Redactor.redact_dump` may undo a substitution a model cannot hold. Every
#: round puts back everything the validator complained about, so one is nearly always enough.
_MAX_UNDO_ROUNDS = 8

#: ``{name: value}`` or ``(name, value)`` pairs — pairs because two independent namespaces may
#: use the same name for different values, and a mapping would keep only one of them.
Secrets = Mapping[str, Any] | Iterable[tuple[str, Any]]

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


def _name_in(replacement: str) -> str:
    """The secret's name inside a :data:`REDACTION` marker — the inverse of formatting one."""
    return replacement[len(_MARKER_PREFIX) : len(replacement) - len(_MARKER_SUFFIX)]


def _variants(value: str) -> tuple[str, ...]:
    """The literal plus the forms a writer may serialise it into.

    A writer of raw TEXT — ``stdout.log``, an artifact, a step output that happens to contain a
    JSON document — sees whatever form the producer wrote, so a value containing a quote, a
    backslash or a newline has to match in its escaped form as well. The records themselves are
    redacted on their parsed VALUES, where only the raw form ever appears; this variant is what
    keeps the text writers covered.
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
    #: Strings this redactor must never rewrite: the addresses the run is RECORDED under
    #: (:data:`IDENTITY_FIELDS_ATTR`). See :meth:`build` for why a secret equal to one of them
    #: is not redacted anywhere rather than in some places.
    identities: frozenset[str] = frozenset()
    #: Names whose value IS one of :attr:`identities` and is therefore not redacted at all.
    collisions: tuple[str, ...] = ()
    #: Length of the longest literal — how much a :class:`StreamRedactor` must hold back.
    max_len: int = field(default=0)

    @classmethod
    def build(
        cls,
        secrets: Secrets,
        *,
        detectors: Sequence[str] = (),
        identities: Iterable[str] = (),
    ) -> Redactor:
        """A redactor for ``{name: value}`` plus the named builtin detectors.

        Non-string values are stringified (an integer secret input is still a secret); ``None``
        and values shorter than :data:`MIN_REDACTABLE_LEN` are skipped.

        ``secrets`` may also be an iterable of ``(name, value)`` PAIRS. A caller that knows two
        independent sets of secrets — the ``config.secrets`` table and the workflow's own
        ``secret: true`` inputs — cannot merge them into one mapping first: the two namespaces
        can use the same name for different values, and the merge would drop one of the values
        from the redactor while the step still receives it.

        ``identities`` are the strings the run is RECORDED under — its id, its workflow's name
        and file, its project root, its workspace (:data:`IDENTITY_FIELDS_ATTR`; the caller
        collects them with :func:`rayspec.store.model.identity_strings`). A secret whose value
        EQUALS one of them produces no literal at all, and its name is recorded in
        :attr:`collisions` for the caller to warn about. Three reasons, in order of weight:

        * the record must keep those strings — ``resume``/``explain``/``approve`` resolve the run
          by them, and :meth:`redact_dump` therefore already writes them in clear. A redactor
          that rewrites them everywhere else does not protect the value, it makes two files of
          one run disagree about the same fact;
        * that disagreement DISCLOSES the secret. ``[REDACTED:token]`` standing where a reader
          can look up the true content one file over (``run.json``, ``rayspec runs``) says
          exactly which public string the secret is. Marking is an oracle here, not a defence;
        * and nothing is being given up, because the string is public in the project already:
          it is a file name, a directory, an id. Redacting a workflow's own name removes it from
          the log and from nowhere else.

        This is the same answer :data:`MIN_REDACTABLE_LEN` gives to the other value redaction
        cannot help with — do not pretend, and name it — and it is given ONCE, here, so every
        writer, sink and console shares it by construction.
        """
        literals: list[tuple[str, str]] = []
        skipped: list[str] = []
        collisions: list[str] = []
        identity = frozenset(identities)
        seen: set[str] = set()
        items: Iterable[tuple[str, Any]] = (
            cast("Mapping[str, Any]", secrets).items() if isinstance(secrets, Mapping) else secrets
        )
        for name, raw in items:
            if raw is None:
                continue
            value = raw if isinstance(raw, str) else str(raw)
            if len(value) < MIN_REDACTABLE_LEN:
                skipped.append(name)
                continue
            if value in identity:
                collisions.append(name)
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
            identities=identity,
            collisions=tuple(dict.fromkeys(collisions)),
            max_len=max((len(n) for n, _ in literals), default=0),
        )

    def __bool__(self) -> bool:
        return bool(self.literals or self.patterns)

    @property
    def hold(self) -> int:
        """UPPER BOUND on what a :class:`StreamRedactor` may hold back for a PARTIAL match.

        Not what it *does* hold back: :meth:`StreamRedactor.feed` holds back only the tail that
        could still grow into a match (usually nothing), so a stream that carries no secret is
        never delayed. This bound is what the documentation quotes as the worst case. It does
        not cover the one case where more is held: a complete match the boundary would
        otherwise cut in half is kept whole, and a run of complete matches that overlap each
        other is held until the run ends.
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
        return self.redact_shapes(self.redact_values(text))

    def redact_values(self, text: str) -> str:
        """``text`` with every known VALUE replaced; the detector shapes are left alone.

        The half :class:`StreamRedactor` applies before it measures its boundary: a known value
        is an exact string, so a complete occurrence can be replaced the moment it is whole,
        whereas a detector shape may still be growing (see :meth:`redact_shapes`).
        """
        if not text or not self.literals:
            return text
        for needle, replacement in self.literals:
            if needle in text:
                text = text.replace(needle, replacement)
        return text

    def redact_shapes(self, text: str) -> str:
        """``text`` with every enabled detector SHAPE replaced; known values are left alone.

        Only ever applied to text a :class:`StreamRedactor` has decided to release: a shape
        matched on a partial buffer would replace the prefix of a token and let its tail
        through.
        """
        if not text or not self.patterns:
            return text
        for name, pattern in self.patterns:
            text = pattern.sub(REDACTION.format(name=name), text)
        return text

    def redact_obj(self, value: Any) -> Any:
        """:meth:`redact` applied to every string inside a JSON-shaped value.

        Mapping KEYS are redacted as well: a structured provider result or a tool argument can
        put a value in the key position (``{"<token>": …}``) just as easily as in the value
        position, and a walk that only visits values writes it out raw. Two keys that redact to
        the same marker collapse into one entry — the same thing that happens to two identical
        values, and the right trade when the alternative is persisting the value. A record's
        own structure is not free-form and is handled separately (see :meth:`redact_dump`).

        A *number* that IS a secret (a numeric account id, a PIN, a numeric token) becomes the
        marker string: redacting the serialised text instead would replace a bare JSON token
        with ``[REDACTED:…]`` and leave an invalid document behind. Only a number whose whole
        text equals a secret is replaced — rewriting the digits *inside* a longer number would
        produce the same broken document.
        """
        if not self:
            return value
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {
                (self.redact(k) if isinstance(k, str) else k): self.redact_obj(v)
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.redact_obj(v) for v in value]
        if isinstance(value, bool) or value is None:
            return value  # `true`/`null` are too common to match a value against
        if isinstance(value, int | float):
            text = str(value)
            for needle, replacement in self.literals:
                if needle == text:
                    return replacement
        return value

    def redact_dump(self, model: BaseModel, *, preserve: Sequence[str] = ()) -> Any:
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

        The record's STRUCTURE is left alone for the same reason: a field name and the key of a
        mapping of records (the run's steps, keyed by step path) name a place in the record
        rather than carry a value, and rewriting one drops the field on the way back in
        (pydantic ignores what it does not know) or points a step at a directory that is not
        there.

        ``preserve`` names the TOP-LEVEL fields that are identity rather than content: the
        strings the record is looked up BY. They are the same class one level up — rewriting one
        does not leak less, it destroys the record just as a rewritten field name would, and
        there is no way to undo it afterwards. The caller decides which they are, because this
        module knows nothing about runs (:data:`rayspec.store.file.RUN_IDENTITY_FIELDS`), and
        its word applies to this level only: a nested record that happens to use the same field
        name is content.

        A nested record declares its own instead, as the ``ClassVar``
        :data:`IDENTITY_FIELDS_ATTR` — honoured wherever that model appears, because the writer
        at the top cannot be expected to name a field five levels down and the record whose
        address it is can. A step's ``path`` is the example: it is the key its own mapping is
        already stored under, so keeping it discloses nothing that the structure did not, while
        rewriting it leaves a record that ``rayspec explain`` cannot even parse.

        Every other structural string is NOT exempt — a secret that equals the workflow hash or
        the project slug is far more likely to be a leak than a coincidence, and neither is what
        a later command resolves the run by.
        """
        data = model.model_dump(mode="json")
        if not self:
            return data
        out = self._redact_record(model, data, preserve=preserve)
        if out == data:  # nothing matched: skip the round trip a checkpoint pays on every save
            return out
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

    def _redact_record(self, model: BaseModel, data: Any, *, preserve: Sequence[str] = ()) -> Any:
        """``data`` (the dump of ``model``) redacted, with the model's own structure intact.

        The model instance is walked beside its dump so that a field name stays a field name;
        everything the record holds that is free-form — ``inputs``, ``outputs``, a toolchain
        payload — goes through :meth:`redact_obj`, whose keys ARE redacted. ``preserve`` (field
        names or their serialisation aliases) is passed through from :meth:`redact_dump` and
        applies to this level only — nested records are reached through :meth:`_redact_child`,
        which does not carry it. What travels with the model instead is its own
        :data:`IDENTITY_FIELDS_ATTR` declaration, read here at whatever depth it turns up.
        """
        if not isinstance(data, dict):
            return self.redact_obj(data)
        cls = type(model)
        fields = {
            (info.serialization_alias or info.alias or name): name
            for name, info in cls.model_fields.items()
        }
        identity = (*preserve, *getattr(cls, IDENTITY_FIELDS_ATTR, ()))
        keep = (
            frozenset(key for key, name in fields.items() if key in identity or name in identity)
            if identity
            else frozenset()
        )
        return {
            key: (
                value
                if key in keep
                else self._redact_child(getattr(model, fields[key], None), value)
                if key in fields
                else self.redact_obj(value)
            )
            for key, value in data.items()
        }

    def _redact_child(self, attr: Any, value: Any) -> Any:
        """One field's dump: descend into nested records, redact anything else whole."""
        if isinstance(attr, BaseModel):
            return self._redact_record(attr, value)
        if isinstance(attr, list | tuple) and isinstance(value, list) and len(attr) == len(value):
            return [self._redact_child(a, v) for a, v in zip(attr, value, strict=True)]
        if (
            isinstance(attr, Mapping)
            and isinstance(value, dict)
            and set(attr) == set(value)
            and any(isinstance(v, BaseModel) for v in attr.values())
        ):  # a mapping of records is addressed by its keys (step path → StepRecord)
            return {k: self._redact_child(attr[k], v) for k, v in value.items()}
        return self.redact_obj(value)

    def covers(self, value: Any) -> bool:
        """True when :meth:`redact` would remove ``value`` from any text it appears in.

        A value shorter than :data:`MIN_REDACTABLE_LEN` counts as covered: it is deliberately
        never redacted, so there is nothing a caller could add to change that. So does a value
        that IS one of this redactor's :attr:`identities` — for the same reason, and because a
        run whose secret happens to equal its own name must still be allowed to start
        (:meth:`uncovered` is what the engine refuses on).
        """
        text = value if isinstance(value, str) else str(value)
        if len(text) < MIN_REDACTABLE_LEN or text in self.identities:
            return True
        return self.redact(text) != text

    def uncovered(self, secrets: Secrets) -> tuple[str, ...]:
        """The NAMES in ``secrets`` whose value :meth:`redact` would still let through.

        The read-back half of installing a redactor: assigning one to a store is not proof that
        it took (a setter may accept the value and drop it), so the caller asks the store for
        what it now holds and checks it here. Empty means every value is covered — including the
        ones deliberately not redacted because they are shorter than
        :data:`MIN_REDACTABLE_LEN`, which nothing could cover and which must therefore never
        read as a failure. Takes the same pairs :meth:`build` does.
        """
        items: Iterable[tuple[str, Any]] = (
            cast("Mapping[str, Any]", secrets).items() if isinstance(secrets, Mapping) else secrets
        )
        missing = [name for name, value in items if value is not None and not self.covers(value)]
        return tuple(dict.fromkeys(missing))

    def with_identities(self, identities: Iterable[str]) -> Redactor:
        """This redactor, told which strings the run is RECORDED under.

        Any literal that IS one of them is dropped and its name moved to :attr:`collisions` —
        the same answer :meth:`build` gives, applied to a redactor that was built before the
        run's own addresses were known. That is the ordinary case for an **embedded** run: the
        engine installs the boundary before the record exists (``docs/extending.md``), so the
        addresses are taught here, in the one step before the first record is written. Returns
        ``self`` when nothing changes, so the CLI — which already knew them at build time — pays
        nothing.
        """
        identity = self.identities | frozenset(identities)
        collided = [pair for pair in self.literals if pair[0] in identity]
        if identity == self.identities and not collided:
            return self
        literals = tuple(pair for pair in self.literals if pair[0] not in identity)
        collisions = tuple(dict.fromkeys(self.collisions + tuple(_name_in(r) for _, r in collided)))
        return replace(
            self,
            literals=literals,
            identities=identity,
            collisions=collisions,
            max_len=max((len(n) for n, _ in literals), default=0),
        )

    def extend(self, secrets: Secrets) -> Redactor:
        """This redactor plus ``{name: value}`` — the same detectors, the union of the literals.

        Used where a value becomes known after the redactor was built (the engine adds the
        run's own secrets to whatever the caller installed). Takes the same pairs
        :meth:`build` does, and applies THIS redactor's :attr:`identities` to them, so a value
        added later gets the same answer as one known from the start. Returns ``self`` when
        there is nothing to add and no new name was skipped or collided, so the common path
        allocates nothing — and a caller can tell from the identity of the result whether this
        redactor already knew everything.
        """
        added = Redactor.build(secrets, identities=self.identities)
        known = {needle for needle, _ in self.literals}
        fresh = [pair for pair in added.literals if pair[0] not in known]
        skipped = tuple(dict.fromkeys(self.skipped + added.skipped))
        collisions = tuple(dict.fromkeys(self.collisions + added.collisions))
        if not fresh and skipped == self.skipped and collisions == self.collisions:
            return self
        literals = sorted([*self.literals, *fresh], key=lambda pair: len(pair[0]), reverse=True)
        return replace(
            self,
            literals=tuple(literals),
            skipped=skipped,
            collisions=collisions,
            max_len=max((len(n) for n, _ in literals), default=0),
        )

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

    Only a SCALAR is ever put back. An error reported against a whole object or list says
    nothing about which of its fields is the problem, and restoring the subtree would put every
    secret inside it back too — better an unreadable record (which :meth:`Redactor.redact_dump`
    warns about) than a readable one with the value in it.
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
        original = container_data[key]
        if isinstance(original, dict | list) or container_out[key] == original:
            continue
        container_out[key] = original
        changed = True
    return changed


#: The shared no-op redactor (a store or sink without secrets uses this).
NULL_REDACTOR = Redactor()


class StreamRedactor:
    """Chunk-boundary-safe redaction of ONE text stream.

    A secret split across two ``text_delta`` chunks (``ghp_SEC`` + ``RET…``) matches neither
    chunk on its own, so :meth:`feed` holds back the tail that could still *grow* into a match —
    the longest suffix that is a proper prefix of a known value, or that a detector's shape is
    still in the middle of — and then moves that boundary further back rather than cutting a
    COMPLETE match in half. Text that cannot be part of any secret is returned immediately, so
    a live log or console tree never lags behind a long-running step. The concatenation of
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
        """The safe prefix of everything fed so far that has not been returned yet.

        Both rules are measured on the RAW buffer, and both only ever move the boundary
        *earlier*: the tail that could still grow into a match is held back, and then the
        boundary is pulled back past any complete match it would otherwise cut in half. The
        second rule is what a self-overlapping value needs: ``4242424242`` ends with its own
        eight-character prefix, so the first rule draws the line two characters in — and those
        two characters would go out raw while the rest waits for a continuation that never
        comes. Substituting complete matches before measuring is NOT an alternative: when one
        known value is a prefix of another (``dbuser`` and ``dbuser:pw@host``) it replaces the
        short one and destroys the very prefix the boundary needed in order to wait for the
        long one.
        """
        if not self._redactor:
            return text
        if not text:
            return ""
        raw = self._pending + text
        cut = self._keep_matches_whole(raw, len(raw) - self._hold(raw))
        if cut <= 0:
            self._pending = raw
            return ""
        self._pending = raw[cut:]
        return self._redactor.redact(raw[:cut])

    def _keep_matches_whole(self, raw: str, cut: int) -> int:
        """``cut`` moved back until no COMPLETE match of a known value straddles it.

        A match that starts before the cut and ends after it would have its head released as
        raw text (the replacement only ever sees ``raw[:cut]``) and its tail replaced later, so
        the whole match is held instead. Matches that overlap each other chain the boundary
        further back, at worst to the start of the buffer — the safe answer.
        """
        while cut > 0:
            earliest = cut
            for needle, _ in self._redactor.literals:
                index = raw.find(needle, max(0, cut - len(needle) + 1))
                if 0 <= index < cut:  # the search start guarantees it ends after the cut
                    earliest = min(earliest, index)
            if earliest == cut:
                return cut
            cut = earliest
        return cut

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
    "IDENTITY_FIELDS_ATTR",
    "MIN_REDACTABLE_LEN",
    "NULL_REDACTOR",
    "PEM_MAX_BODY",
    "REDACTION",
    "RedactingSink",
    "Redactor",
    "Secrets",
    "StreamRedactor",
    "detector_patterns",
]
