# SPDX-License-Identifier: Apache-2.0
"""Redactor properties: what a writer emits never contains a value the redactor was given.

The redactor is the last thing between a secret and a file, a log line or a sink, and it was
the only named mutation target (``tests/properties/mutation/mutate.py``) with no generated
coverage at all — every case it had was a value somebody thought of. The three promises below
are the ones its docstring makes, and each is a claim about *all* text rather than about the
examples:

1. **nothing known survives** — no value the redactor holds is left in what it returns, in its
   raw form or in the escaped form a text writer produces (``\\n``, ``\\"``, ``\\\\``);
2. **the chunking cannot matter** — a :class:`StreamRedactor` fed the same text in any
   chunking returns the same thing as redacting the whole text at once. That is the promise a
   live log depends on, and the one a hand-written test only ever checks at the boundaries
   somebody imagined;
3. **structure is walked, not serialised** — ``redact_obj`` reaches strings in keys, in nested
   containers and a number whose whole text is a secret.

Values shorter than :data:`~rayspec.redact.MIN_REDACTABLE_LEN` are documented as never
redacted, so the generator never draws one: a property has to describe the promise the code
makes, not the one a reader wishes it made.
"""

from __future__ import annotations

import json
import random
import string
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

import pytest

from rayspec.redact import (
    MIN_REDACTABLE_LEN,
    REDACTION,
    Redactor,
    detector_patterns,
)

from .generate import Discard, forall, shrink_seq, text

#: The detectors the opt-in shapes cover, and how a token of each is drawn.
DETECTORS: tuple[str, ...] = ("github", "openai", "aws", "jwt", "pem")
#: Secret names. Short and boring on purpose: the marker is ``[REDACTED:<name>]`` and a name
#: that could itself contain a generated value would make the properties argue with themselves.
NAMES: tuple[str, ...] = ("s0", "s1", "s2")
#: What a blanked marker becomes while a property looks for leftovers. NUL, because
#: :data:`~tests.properties.generate.FRAGMENTS` deliberately never produces one, so blanking
#: cannot manufacture the very occurrence the property is hunting for.
BLANK = "\x00"


def secret_value(rng: random.Random) -> str:
    """A generated value long enough to be redactable at all (see ``MIN_REDACTABLE_LEN``)."""
    for _ in range(20):
        value = text(rng, max_parts=4)
        if len(value) >= MIN_REDACTABLE_LEN:
            return value
    return text(rng, max_parts=2) + "abcd"


def detector_token(rng: random.Random, name: str) -> str:
    """A string that matches the builtin detector ``name`` — generated, not a fixed example."""
    alnum = string.ascii_letters + string.digits

    def run(alphabet: str, low: int, high: int) -> str:
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(low, high)))

    if name == "github":
        return rng.choice(("ghp_", "gho_", "ghu_", "ghs_", "ghr_")) + run(alnum, 16, 40)
    if name == "openai":
        return "sk-" + ("proj-" if rng.random() < 0.5 else "") + run(alnum + "_-", 16, 40)
    if name == "aws":
        return rng.choice(("AKIA", "ASIA")) + run(string.ascii_uppercase + string.digits, 16, 16)
    if name == "jwt":
        return ".".join(
            "eyJ" + run(alnum + "_-", 6, 20) if i == 0 else run(alnum + "_-", 6, 20)
            for i in range(3)
        )
    body = run(alnum + "+/=\n", 20, 200)
    return f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"


@dataclass(frozen=True)
class RedactCase:
    """Secrets, the chunks a stream is fed, and the detectors that are switched on."""

    secrets: tuple[tuple[str, str], ...]
    chunks: tuple[str, ...]
    detectors: tuple[str, ...]

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(value for _, value in self.secrets)

    @property
    def whole(self) -> str:
        return "".join(self.chunks)

    def redactor(self) -> Redactor:
        return Redactor.build(self.secrets, detectors=self.detectors)

    def __str__(self) -> str:
        return f"secrets={list(self.secrets)} detectors={list(self.detectors)} {self.chunks!r}"


def _cut(rng: random.Random, value: str) -> tuple[str, ...]:
    """``value`` split into chunks at random points — empty chunks included, a stream sees those."""
    cuts = sorted(rng.randint(0, len(value)) for _ in range(rng.randint(0, 4)))
    out: list[str] = []
    last = 0
    for point in cuts:
        out.append(value[last:point])
        last = point
    out.append(value[last:])
    return tuple(out)


def _haystack(rng: random.Random, values: Sequence[str], detectors: Sequence[str]) -> str:
    """Text that really holds the secrets — raw, escaped, and buried in filler."""
    pieces: list[str] = []
    for _ in range(rng.randint(1, 6)):
        roll = rng.random()
        if roll < 0.45:
            pieces.append(rng.choice(values))
        elif roll < 0.6:
            # the form a text writer produces: a log line, an artifact, a JSON document
            pieces.append(json.dumps(rng.choice(values))[1:-1])
        elif roll < 0.75 and detectors:
            pieces.append(detector_token(rng, rng.choice(detectors)))
        else:
            pieces.append(text(rng, max_parts=3))
    return "".join(pieces)


def redact_case(rng: random.Random) -> RedactCase:
    """Secrets, a text that contains them, and a chunking of it."""
    secrets = tuple((NAMES[i], secret_value(rng)) for i in range(rng.randint(1, len(NAMES))))
    detectors = tuple(name for name in DETECTORS if rng.random() < 0.4)
    values = tuple(value for _, value in secrets)
    return RedactCase(
        secrets=secrets,
        chunks=_cut(rng, _haystack(rng, values, detectors)),
        detectors=detectors,
    )


#: The chunkings that must be checked whatever the seed. Each one is a boundary the generator
#: reaches only when the RNG puts a cut in one particular place, and each one broke something:
#: a value split across chunks, a value whose own prefix is its suffix, and a detector shape
#: followed by enough continuation characters to push the hold window past its own start —
#: which released ``AKIA0123456789ABCDEF`` into a log in the clear.
LANDMINES: tuple[RedactCase, ...] = (
    RedactCase(
        secrets=(("s0", "hunter2-secret"),), chunks=("hun", "ter2-se", "cret"), detectors=()
    ),
    RedactCase(secrets=(("s0", "4242424242"),), chunks=("[4242424242]",), detectors=()),
    RedactCase(
        secrets=(("s0", "unrelated-value"),), chunks=("AKIA0123456789ABCDEF01",), detectors=("aws",)
    ),
    RedactCase(
        secrets=(("s0", "unrelated-value"),),
        chunks=("AKIA0123456789ABCDE", "F0123456789"),
        detectors=("aws",),
    ),
    RedactCase(
        secrets=(("s0", "unrelated-value"),),
        chunks=("ghp_0123456789abcdefgh", "sk-proj-0123456789abcdef"),
        detectors=("github", "openai"),
    ),
)


def shrink_case(case: RedactCase) -> Iterator[RedactCase]:
    """Fewer chunks, then fewer detectors, then fewer secrets (never none of either)."""
    for smaller in shrink_seq(case.chunks):
        if smaller:
            yield replace(case, chunks=tuple(smaller))
    for smaller in shrink_seq(case.detectors):
        yield replace(case, detectors=tuple(smaller))
    for smaller in shrink_seq(case.secrets):
        if smaller:
            yield replace(case, secrets=tuple(smaller))


def forms(value: str) -> tuple[str, ...]:
    """The forms of a value a writer can produce: as written, and as ``json.dumps`` escapes it.

    Spelled out here rather than imported from ``rayspec.redact._variants``: an oracle that asks
    the module under test which forms it covers agrees with it by construction, and a leak of
    the escaped form is exactly what these properties have to be able to see.
    """
    escaped = json.dumps(value)[1:-1]
    return (value,) if escaped == value else (value, escaped)


def leftovers(case: RedactCase, redacted: str) -> list[str]:
    """The secret values still readable in ``redacted``, markers blanked out first.

    Blanking is what makes the question honest: ``[REDACTED:s0]`` is text the redactor ADDED,
    and a value that only appears inside (or across the seam of) a marker was never left in —
    it was written there by the substitution itself. Both :func:`forms` count as readable: a
    log line that carries ``a\\nb`` has the secret in it just as much as one that carries the
    newline, and looking only for the raw form is how a property misses the escaped half.
    """
    for name, _ in case.secrets:
        redacted = redacted.replace(REDACTION.format(name=name), BLANK)
    for name in case.detectors:
        redacted = redacted.replace(REDACTION.format(name=name), BLANK)
    return [form for value in case.values for form in forms(value) if form and form in redacted]


# --------------------------------------------------------------------------------------------------
# 1. nothing known survives
# --------------------------------------------------------------------------------------------------


def test_no_known_value_survives_a_redaction() -> None:
    """Whatever the text, no value the redactor holds is readable in what it returns.

    Both forms: the value as written, and the escaped form ``json.dumps`` produces — a step
    output that happens to be a JSON document carries the secret as ``a\\nb``, and a redactor
    that only knew the raw form would write it out in full.
    """
    covered = 0

    def prop(case: RedactCase) -> None:
        nonlocal covered
        redactor = case.redactor()
        if not any(value and value in case.whole for value in case.values):
            raise Discard("this text does not contain any of the secrets")
        covered += 1
        left = leftovers(case, redactor.redact(case.whole))
        assert not left, f"{left!r} survived {case}"

    forall("redact-values", redact_case, prop, cases=120, shrink=shrink_case, show=str)
    assert covered > 0, "no generated text ever contained a secret"


def test_an_escaped_occurrence_is_redacted_too() -> None:
    """A text that carries ONLY the escaped form, so the raw form cannot carry the property.

    ``stdout.log`` and a step output that happens to be a JSON document see the value as the
    producer wrote it, which for anything holding a quote, a backslash or a newline is
    ``json.dumps``'s form and not the value. Drawn from the values that actually differ when
    escaped — a case where the two forms are the same says nothing about this.
    """
    covered = 0

    def prop(case: RedactCase) -> None:
        nonlocal covered
        escapable = [value for value in case.values if len(forms(value)) == 2]
        if not escapable:
            raise Discard("no generated value in this case changes when it is escaped")
        covered += 1
        redactor = case.redactor()
        only_escaped = " ".join(json.dumps(value)[1:-1] for value in escapable)
        left = leftovers(case, redactor.redact(only_escaped))
        assert not left, f"{left!r} survived the escaped form of {case}"

    forall("redact-escaped", redact_case, prop, cases=60, shrink=shrink_case, show=str)
    assert covered > 0, "no generated value ever needed escaping"


def test_a_value_too_short_to_redact_is_named_rather_than_dropped() -> None:
    """The documented limit, stated as a property: skipped, and the name says which."""

    def prop(case: RedactCase) -> None:
        short = tuple((name, value[: MIN_REDACTABLE_LEN - 1]) for name, value in case.secrets)
        redactor = Redactor.build(short)
        assert set(redactor.skipped) == {name for name, _ in short}
        assert not redactor.literals, "a value below the threshold must not become a needle"
        # `uncovered` is the read-back a caller uses to prove the redactor took: a value that
        # is deliberately never redacted must not read as a failure there.
        assert redactor.uncovered(short) == ()

    forall("redact-short", redact_case, prop, cases=40, shrink=shrink_case, show=str)


# --------------------------------------------------------------------------------------------------
# 2. the chunking cannot matter
# --------------------------------------------------------------------------------------------------


def test_a_stream_redacts_the_same_however_it_is_chunked() -> None:
    """``"".join(feed(c) for c in chunks) + flush() == redact("".join(chunks))``.

    The documented promise, values and detector shapes together, and the only property that
    covers a secret split across two ``text_delta`` chunks — the shape a live log actually
    produces and the one a fixed example can only ever hit where its author placed the cut.
    """
    split = 0

    def prop(case: RedactCase) -> None:
        nonlocal split
        redactor = case.redactor()
        stream = redactor.stream()
        out = "".join(stream.feed(chunk) for chunk in case.chunks) + stream.flush()
        assert out == redactor.redact(case.whole), (
            f"chunked {out!r} != whole {redactor.redact(case.whole)!r} for {case}"
        )
        split += any(
            value and any(value not in chunk and value in case.whole for chunk in case.chunks)
            for value in case.values
        )
        assert not leftovers(case, out), f"the stream let a value through: {case}"

    forall(
        "redact-stream",
        redact_case,
        prop,
        cases=150,
        shrink=shrink_case,
        show=str,
        examples=LANDMINES,
    )
    assert split > 0, "no generated case ever split a value across a chunk boundary"


def test_a_stream_never_releases_a_detector_shape() -> None:
    """With detectors on, no shape reaches the reader — whatever the chunking.

    Stated separately from the equality above because it is the claim that matters: equality
    would also be satisfied by a stream that let every shape through, as long as the whole-text
    redaction let it through too. This is the half that says a credential does not reach the
    console or ``stdout.log``, and it is the half that was false — a stream released
    ``AKIA0123456789ABCDEF`` in the clear whenever two more ``[0-9A-Z]`` characters followed it.
    """
    covered = 0

    def prop(case: RedactCase) -> None:
        nonlocal covered
        if not case.detectors:
            raise Discard("no detector is enabled in this case")
        redactor = case.redactor()
        stream = redactor.stream()
        out = "".join(stream.feed(chunk) for chunk in case.chunks) + stream.flush()
        covered += 1
        for name, pattern in detector_patterns(case.detectors):
            assert not pattern.search(out), f"{name} shape survived the stream: {out!r} ({case})"
        assert not leftovers(case, out), f"the stream let a value through: {case}"

    forall(
        "redact-stream-shapes",
        redact_case,
        prop,
        cases=120,
        shrink=shrink_case,
        show=str,
        examples=LANDMINES,
    )
    assert covered > 0, "no generated case ever enabled a detector"


def test_a_stream_that_is_never_flushed_holds_the_tail_back() -> None:
    """The other half of the contract: what ``feed`` withheld is only ever a tail."""

    def prop(case: RedactCase) -> None:
        redactor = Redactor.build(case.secrets)
        stream = redactor.stream()
        released = "".join(stream.feed(chunk) for chunk in case.chunks)
        assert not leftovers(case, released), f"a value was released unredacted: {case}"
        assert redactor.redact(case.whole).startswith(released) or not released, (
            f"the released prefix is not a prefix of the answer: {released!r}"
        )

    forall("redact-stream-prefix", redact_case, prop, cases=80, shrink=shrink_case, show=str)


# --------------------------------------------------------------------------------------------------
# 3. structure is walked, not serialised
# --------------------------------------------------------------------------------------------------


def nested(case: RedactCase) -> Any:
    """A JSON-shaped value with the secrets in every position a walk has to reach."""
    values = list(case.values)
    return {
        values[0]: "in a key",
        "list": [{"deep": value} for value in values],
        "text": " ".join(values),
        "tuple": tuple(values),
    }


def test_redact_obj_reaches_keys_and_nested_values() -> None:
    """A structured provider result can put the value in the key position just as easily."""

    def prop(case: RedactCase) -> None:
        redactor = case.redactor()
        out = redactor.redact_obj(nested(case))
        assert not leftovers(case, json.dumps(out, ensure_ascii=False)), f"{out!r} for {case}"

    forall("redact-obj", redact_case, prop, cases=80, shrink=shrink_case, show=str)


def test_a_numeric_secret_becomes_the_marker_rather_than_broken_json() -> None:
    """A number whose whole text is a secret is replaced as a VALUE, so the document still parses."""

    def prop(numbers: list[int]) -> None:
        secrets = tuple((f"n{i}", n) for i, n in enumerate(numbers))
        redactor = Redactor.build(secrets)
        out = redactor.redact_obj({"a": list(numbers), "b": {str(numbers[0]): numbers[0]}})
        assert json.loads(json.dumps(out)) == out
        assert out["a"] == [REDACTION.format(name=f"n{i}") for i in range(len(numbers))]

    def gen(rng: random.Random) -> list[int]:
        return [rng.randint(10**3, 10**12) for _ in range(rng.randint(1, 3))]

    forall("redact-numbers", gen, prop, cases=40, shrink=shrink_seq)


# --------------------------------------------------------------------------------------------------
# 4. the opt-in shapes
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", DETECTORS)
def test_a_detector_shape_never_survives_however_it_is_chunked(name: str) -> None:
    """An enabled detector catches its shape whole, and across any chunk boundary.

    Generated per detector rather than once over a random one: a detector nobody drew is a
    detector nobody checked, and which detector the RNG picks is not something a test should
    leave to the seed.
    """
    pattern = dict(detector_patterns([name]))[name]

    def prop(case: RedactCase) -> None:
        token = detector_token(random.Random(str(case.chunks)), name)
        redactor = Redactor.build((), detectors=[name])
        body = f"{case.whole}{token}{case.whole}"
        whole = redactor.redact(body)
        assert REDACTION.format(name=name) in whole, f"{token!r} was not caught at all"
        assert not pattern.search(whole), f"{name} shape survived the whole text: {whole!r}"
        stream = redactor.stream()
        chunks = _cut(random.Random(str(case.secrets)), body)
        out = "".join(stream.feed(chunk) for chunk in chunks) + stream.flush()
        assert REDACTION.format(name=name) in out, f"{token!r} was not caught when chunked"
        assert not pattern.search(out), f"{name} shape survived a chunking: {out!r}"
        assert out == whole, f"chunked {out!r} != whole {whole!r}"

    forall(f"redact-detector-{name}", redact_case, prop, cases=40, shrink=shrink_case, show=str)
