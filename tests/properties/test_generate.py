# SPDX-License-Identifier: Apache-2.0
"""The property driver itself: determinism, reporting and shrinking.

A generative suite is only worth its runtime if a failure is *reproducible* and *minimal*, so
those two promises get their own tests. Without them a red property test is a rumour.
"""

from __future__ import annotations

import ast
import random
from collections.abc import Iterator

import pytest

from .generate import (
    Discard,
    Falsified,
    NoCases,
    aforall,
    forall,
    json_value,
    raises,
    rng_for,
    seed_key,
    shrink_json,
    shrink_seq,
    shrink_text,
    text,
)

pytestmark = pytest.mark.anyio


def test_the_same_seed_key_draws_the_same_case() -> None:
    """Two runs of one property must see identical cases — the whole point of the seed key."""
    first = [text(rng_for("demo", i)) for i in range(20)]
    second = [text(rng_for("demo", i)) for i in range(20)]
    assert first == second
    assert first != [text(rng_for("demo", i, seed=1)) for i in range(20)]


def test_a_holding_property_draws_every_case() -> None:
    """A property that holds is asked about every case, not just the first."""
    seen: list[int] = []
    forall("count", lambda rng: rng.randint(0, 9), lambda v: seen.append(v), cases=25)
    assert len(seen) == 25


def test_a_falsified_property_names_the_seed_and_the_minimal_case() -> None:
    """The report has to carry a key that reproduces the case and the smallest input."""

    def prop(value: str) -> None:
        assert "\n" not in value, "newlines are not allowed"

    with pytest.raises(Falsified) as info:
        forall("nl", lambda rng: text(rng), prop, cases=200, shrink=shrink_text)
    message = str(info.value)
    assert "property 'nl' falsified" in message
    assert "seed key:     nl#0#" in message
    assert "minimal case: '\\n'" in message, message
    assert "newlines are not allowed" in message
    key = message.split("seed key:")[1].split("\n")[0].strip()
    index = int(key.rsplit("#", 1)[1])
    assert key == seed_key("nl", index)
    assert "\n" in text(rng_for("nl", index)), "the printed key must reproduce the failing case"


def test_shrinking_is_bounded() -> None:
    """A property nothing can satisfy must not shrink for ever."""
    calls = 0

    def prop(value: list[int]) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("always")

    def endless(value: list[int]) -> Iterator[list[int]]:
        while True:
            yield [*value, 0]

    with pytest.raises(Falsified):
        forall("bounded", lambda rng: [1], prop, cases=1, shrink=endless, budget=17)
    assert calls == 1 + 17


def test_without_a_shrinker_the_original_case_is_reported() -> None:
    """No shrinker is a valid choice — the report then quotes the drawn case unchanged."""

    def prop(value: int) -> None:
        raise AssertionError("nope")

    with pytest.raises(Falsified, match=r"minimal case: 7"):
        forall("noshrink", lambda rng: 7, prop, cases=1)


async def test_aforall_reports_like_forall() -> None:
    """The async driver seeds, shrinks and reports the same way the sync one does."""

    async def prop(value: list[int]) -> None:
        assert 3 not in value

    with pytest.raises(Falsified) as info:
        await aforall(
            "async",
            lambda rng: [rng.randint(0, 4) for _ in range(6)],
            prop,
            cases=60,
            shrink=shrink_seq,
        )
    assert "minimal case: [3]" in str(info.value)


def test_shrinkers_only_produce_simpler_values() -> None:
    """A shrinker that can return its own input turns the shrink loop into an infinite one."""
    rng = random.Random("shrink-sanity")
    for _ in range(200):
        value = text(rng)
        for candidate in shrink_text(value):
            assert len(candidate) < len(value)
        jvalue = json_value(rng)
        for candidate in shrink_json(jvalue):
            assert candidate != jvalue


def _minimal(message: str) -> object:
    """The case the report calls minimal, parsed back out of the message."""
    return ast.literal_eval(message.split("minimal case: ")[1].split("\n")[0])


def test_a_discarded_candidate_is_never_reported_as_the_minimal_case() -> None:
    """Shrinking past a precondition would report a case the property does not even cover."""

    def prop(value: str) -> None:
        if len(value) < 3:
            raise Discard("the generator never draws anything shorter")
        assert "\n" not in value, "newlines are not allowed"

    with pytest.raises(Falsified) as info:
        forall("discard", lambda rng: text(rng, max_parts=4), prop, cases=200, shrink=shrink_text)
    message = str(info.value)
    assert "newlines are not allowed" in message, message
    assert "the generator never draws anything shorter" not in message, message
    minimal = _minimal(message)
    assert isinstance(minimal, str) and len(minimal) >= 3, message


def test_a_property_that_discards_every_case_is_an_error() -> None:
    """A precondition no drawn case satisfies is a broken generator, not a green property."""

    def prop(value: int) -> None:
        raise Discard("never applicable")

    with pytest.raises(NoCases, match="discarded all 5"):
        forall("all-discarded", lambda rng: 1, prop, cases=5)


async def test_the_async_driver_discards_the_same_way() -> None:
    """``aforall`` shares the rule: a discard is not a counter-example and not a pass."""

    async def prop(value: int) -> None:
        raise Discard("never applicable")

    with pytest.raises(NoCases, match="discarded all 4"):
        await aforall("async-discarded", lambda rng: 1, prop, cases=4)


def test_raises_keeps_the_exception_and_fails_as_an_assertion() -> None:
    """The property-body form of ``pytest.raises``: the miss is an ordinary ``AssertionError``.

    ``pytest.raises`` fails with ``Failed``, which derives from ``BaseException`` and therefore
    travels straight past the driver — unshrunk, and without the seed key.
    """
    with raises(ValueError) as caught:
        raise ValueError("boom")
    assert str(caught.value) == "boom"
    with pytest.raises(AssertionError, match="expected ValueError"):
        with raises(ValueError):
            pass


def test_a_missing_exception_inside_a_property_is_shrunk_and_reported() -> None:
    """Which is the whole point: the miss reaches the report with a seed key and a minimal case."""

    def prop(value: str) -> None:
        with raises(ValueError):
            if "\n" in value:
                raise ValueError(value)

    with pytest.raises(Falsified) as info:
        forall("raises-shrink", text, prop, cases=50, shrink=shrink_text)
    assert _minimal(str(info.value)) == "", str(info.value)
