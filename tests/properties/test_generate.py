# SPDX-License-Identifier: Apache-2.0
"""The property driver itself: determinism, reporting and shrinking.

A generative suite is only worth its runtime if a failure is *reproducible* and *minimal*, so
those two promises get their own tests. Without them a red property test is a rumour.

**Every seed in this file is written down here.** These tests are about the driver, not about
hunting counter-examples, so none of them may read the ambient ``RAYSPEC_PROP_SEED``: a
self-test that quotes ``BASE_SEED`` back at itself turns the documented seed hunt
(``RAYSPEC_PROP_SEED=7 uv run pytest tests/properties``) red on the first try, which teaches
everyone that the hunt is broken rather than that the code is.
:func:`test_the_driver_self_test_passes_at_any_seed` is what keeps that true — it re-runs this
whole file in a child process under a different seed.
"""

from __future__ import annotations

import ast
import os
import random
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from .generate import (
    BadEnvironment,
    Discard,
    Falsified,
    NoCases,
    aforall,
    forall,
    json_value,
    raises,
    read_environment,
    rng_for,
    seed_key,
    shrink_json,
    shrink_seq,
    shrink_text,
    text,
)

pytestmark = pytest.mark.anyio

#: The seeds this file states for itself, never the ambient one (see the module docstring).
SEED, OTHER_SEED = 4242, 4243
#: Set in the child of :func:`test_the_driver_self_test_passes_at_any_seed` so it does not
#: recurse. Deliberately outside ``RAYSPEC_PROP_*``: the driver refuses unknown names there.
CHILD_ENV = "RAYSPEC_SELFTEST_CHILD"


def test_the_same_seed_key_draws_the_same_case() -> None:
    """Two runs of one property must see identical cases — the whole point of the seed key."""
    first = [text(rng_for("demo", i, seed=SEED)) for i in range(20)]
    second = [text(rng_for("demo", i, seed=SEED)) for i in range(20)]
    assert first == second
    assert first != [text(rng_for("demo", i, seed=OTHER_SEED)) for i in range(20)]


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
        forall("nl", lambda rng: text(rng), prop, cases=200, seed=SEED, shrink=shrink_text)
    message = str(info.value)
    assert "property 'nl' falsified" in message
    assert f"seed key:     nl#{SEED}#" in message, message
    assert "minimal case: '\\n'" in message, message
    assert "newlines are not allowed" in message
    key = message.split("seed key:")[1].split("\n")[0].strip()
    index = int(key.rsplit("#", 1)[1])
    assert key == seed_key("nl", index, seed=SEED)
    assert "\n" in text(rng_for("nl", index, seed=SEED)), (
        "the printed key must reproduce the failing case"
    )


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
        forall("bounded", lambda rng: [1], prop, cases=1, seed=SEED, shrink=endless, budget=17)
    assert calls == 1 + 17


def test_without_a_shrinker_the_original_case_is_reported() -> None:
    """No shrinker is a valid choice — the report then quotes the drawn case unchanged."""

    def prop(value: int) -> None:
        raise AssertionError("nope")

    with pytest.raises(Falsified, match=r"minimal case: 7"):
        forall("noshrink", lambda rng: 7, prop, cases=1, seed=SEED)


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
            seed=SEED,
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
        forall(
            "discard",
            lambda rng: text(rng, max_parts=4),
            prop,
            cases=200,
            seed=SEED,
            shrink=shrink_text,
        )
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
        forall("all-discarded", lambda rng: 1, prop, cases=5, seed=SEED)


async def test_the_async_driver_discards_the_same_way() -> None:
    """``aforall`` shares the rule: a discard is not a counter-example and not a pass."""

    async def prop(value: int) -> None:
        raise Discard("never applicable")

    with pytest.raises(NoCases, match="discarded all 4"):
        await aforall("async-discarded", lambda rng: 1, prop, cases=4, seed=SEED)


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
        forall("raises-shrink", text, prop, cases=50, seed=SEED, shrink=shrink_text)
    assert _minimal(str(info.value)) == "", str(info.value)


# --------------------------------------------------------------------------------------------------
# a property that checks nothing
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cases", [0, -1])
def test_a_property_asked_for_no_cases_is_an_error(cases: int) -> None:
    """The total rule: whatever computed ``cases``, a run that draws none is not a pass.

    An environment variable is only one way to get here. ``DEFAULT_CASES // 4``, an edited
    constant and a caller's arithmetic are the others, and none of them would be caught by a
    check that only guards the variable.
    """
    seen: list[int] = []
    with pytest.raises(NoCases, match=f"asked for cases={cases}"):
        forall("vacuous", lambda rng: 1, lambda v: seen.append(v), cases=cases, seed=SEED)
    assert seen == [], "the property body must not run at all"


@pytest.mark.parametrize("cases", [0, -1])
async def test_the_async_driver_refuses_no_cases_too(cases: int) -> None:
    """``aforall`` shares the rule; the scheduler properties are the ones that compute ``cases``."""

    async def prop(value: int) -> None:
        raise AssertionError("must never run")

    with pytest.raises(NoCases, match=f"asked for cases={cases}"):
        await aforall("vacuous-async", lambda rng: 1, prop, cases=cases, seed=SEED)


# --------------------------------------------------------------------------------------------------
# the RAYSPEC_PROP_* namespace
# --------------------------------------------------------------------------------------------------


def test_the_default_environment_is_the_documented_one() -> None:
    """No variable set: base seed 0 (so CI is reproducible), 60 cases per property."""
    assert read_environment({}) == (0, 60)
    assert read_environment({"RAYSPEC_PROP_SEED": "7", "RAYSPEC_PROP_CASES": "5"}) == (7, 5)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_case_count_is_refused_by_name(value: str) -> None:
    """``RAYSPEC_PROP_CASES=0`` used to switch the whole generative suite off in silence.

    Every property then drew nothing, asserted nothing and passed; one test noticed by accident,
    through a coverage counter that happened to be zero. The message has to name the variable
    AND the value, because the person reading it is looking at a green suite.
    """
    with pytest.raises(BadEnvironment) as info:
        read_environment({"RAYSPEC_PROP_CASES": value})
    message = str(info.value)
    assert "RAYSPEC_PROP_CASES" in message and repr(value) in message, message
    assert "checks nothing and passes" in message, message


@pytest.mark.parametrize("value", ["", "x", "1.5", "6 0"])
def test_a_case_count_that_is_not_an_integer_is_refused_by_name(value: str) -> None:
    """Including the empty string: ``RAYSPEC_PROP_CASES= uv run pytest`` is a typo, not a default."""
    with pytest.raises(BadEnvironment) as info:
        read_environment({"RAYSPEC_PROP_CASES": value})
    assert "RAYSPEC_PROP_CASES" in str(info.value) and repr(value) in str(info.value)


def test_a_seed_that_is_not_an_integer_is_refused_by_name() -> None:
    """Any integer is a usable seed; a value that is not one would fail with a bare ValueError."""
    with pytest.raises(BadEnvironment, match="RAYSPEC_PROP_SEED"):
        read_environment({"RAYSPEC_PROP_SEED": "latest"})
    assert read_environment({"RAYSPEC_PROP_SEED": "-3"}) == (-3, 60)


def test_a_variable_this_driver_does_not_read_is_refused() -> None:
    """A typo in this namespace changes nothing and looks like it changed something.

    ``RAYSPEC_PROP_CASE=500`` is the same defect as ``RAYSPEC_PROP_CASES=0`` seen from the other
    side: the run uses the default, the report says green, and the person who typed it believes
    they went looking. Owning the namespace is what makes the rule total — it does not depend on
    anybody remembering to check a new variable's spelling.
    """
    with pytest.raises(BadEnvironment) as info:
        read_environment({"RAYSPEC_PROP_CASE": "500"})
    message = str(info.value)
    assert "RAYSPEC_PROP_CASE=" in message and "RAYSPEC_PROP_CASES" in message, message


# --------------------------------------------------------------------------------------------------
# examples: the cases a property must check whatever the RNG does
# --------------------------------------------------------------------------------------------------


def test_examples_are_checked_before_the_drawn_cases() -> None:
    """A written example is not a hint to the generator, it is a case the property must cover."""
    seen: list[str] = []
    forall(
        "with-examples",
        lambda rng: "drawn",
        lambda v: seen.append(v),
        cases=3,
        seed=SEED,
        examples=("first", "second"),
    )
    assert seen == ["first", "second", "drawn", "drawn", "drawn"]


def test_a_failing_example_is_reported_as_an_example_not_as_a_seed() -> None:
    """No seed reproduces an example, so the report must not print one that does not work."""

    def prop(value: str) -> None:
        assert value != "landmine", "the example is the counter-example"

    with pytest.raises(Falsified) as info:
        forall("example-fails", lambda rng: "ok", prop, cases=5, seed=SEED, examples=("landmine",))
    message = str(info.value)
    assert "example-fails#example#0" in message, message
    assert "examples[0] of property 'example-fails'" in message, message
    assert f"#{SEED}#" not in message, message


async def test_the_async_driver_checks_examples_too() -> None:
    """``aforall`` shares the feature: the scheduler properties are where the rows are rare."""
    seen: list[str] = []

    async def prop(value: str) -> None:
        seen.append(value)

    await aforall("async-examples", lambda rng: "drawn", prop, cases=2, seed=SEED, examples=("e",))
    assert seen == ["e", "drawn", "drawn"]


# --------------------------------------------------------------------------------------------------
# the guard that keeps every seed in this file written down
# --------------------------------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get(CHILD_ENV) == "1", reason="the child of the seed-independence guard"
)
def test_the_driver_self_test_passes_at_any_seed() -> None:
    """Re-run this whole file under a different ``RAYSPEC_PROP_SEED``; it must still be green.

    The documented way to go looking for counter-examples is ``RAYSPEC_PROP_SEED=<n> uv run
    pytest tests/properties``. Before this guard, the first thing that workflow did was turn the
    DRIVER red — the self-test quoted seed 0 back at itself — so the seed hunt taught its user
    that the hunt was broken. Checked in a child process rather than by inspection, because the
    dependency is on an import-time constant and no assertion inside this process can see it.
    """
    repo = Path(__file__).resolve().parents[2]
    child = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/properties/test_generate.py", "-q", "--no-header"],
        cwd=repo,
        env={
            **os.environ,
            "RAYSPEC_PROP_SEED": str(OTHER_SEED),
            CHILD_ENV: "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert child.returncode == 0, (
        f"the driver's self-test depends on the ambient seed:\n{child.stdout}\n{child.stderr}"
    )
