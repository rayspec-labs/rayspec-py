# SPDX-License-Identifier: Apache-2.0
"""The property driver itself: determinism, reporting and shrinking.

A generative suite is only worth its runtime if a failure is *reproducible* and *minimal*, so
those two promises get their own tests. Without them a red property test is a rumour.
"""

from __future__ import annotations

import random
from collections.abc import Iterator

import pytest

from .generate import (
    Falsified,
    aforall,
    forall,
    json_value,
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
