# SPDX-License-Identifier: Apache-2.0
"""A seeded generative-testing driver: :func:`forall` / :func:`aforall`, plus value generators.

Module boundary: no rayspec import. Everything here is generation, shrinking and reporting.

Why hand-rolled: rayspec ships no test-only generative dependency, and the two things a
property suite actually needs are small — a *reproducible* case (one string seed per case,
printed on failure) and a *minimal* one (greedy shrinking towards the simplest input that still
falsifies the property). ``forall`` gives both in about as much code as configuring a library
would take.

Reproducing a failure::

    property 'shell slots round-trip' falsified after 8 case(s)
      seed key:     shell-round-trip#0#7
      minimal case: 'a\\n'

    RAYSPEC_PROP_SEED=<n> re-runs every property with a different base seed;
    RAYSPEC_PROP_CASES=<n> changes how many cases each property draws.

The base seed is fixed (``0``) so CI is deterministic: a property suite that draws different
cases on every run reports failures nobody can reproduce, and turns a red build into folklore.
Raise ``RAYSPEC_PROP_SEED`` locally to go looking for new counter-examples — the driver's own
self-test states every seed it depends on, so a seed hunt never turns the driver red.

Two rules keep a property from passing without checking anything, which is the failure mode a
generative suite has and a hand-written one does not:

* :func:`forall` / :func:`aforall` REFUSE a non-positive ``cases``, wherever it came from — an
  environment variable, a division, a caller's arithmetic. That is the total rule; the
  environment check below is only the part that can also say what to type instead.
* the driver owns the whole ``RAYSPEC_PROP_*`` namespace (:func:`read_environment`): a value
  that would switch a property off is an error at import, and so is a name in that namespace
  that the driver does not read — a typo that silently changes nothing is the same defect.
"""

from __future__ import annotations

import contextlib
import os
import random
from collections.abc import Awaitable, Callable, Generator, Iterator, Mapping, Sequence
from typing import Any, Generic, TypeVar

T = TypeVar("T")
E = TypeVar("E", bound=BaseException)


class BadEnvironment(RuntimeError):
    """A ``RAYSPEC_PROP_*`` variable the driver cannot use; raised at import, naming the value."""


#: The variables this driver reads: ``name -> (smallest usable value or None, what it means)``.
#: The driver owns the whole ``RAYSPEC_PROP_*`` namespace — see :func:`read_environment`.
ENV_RULES: dict[str, tuple[int | None, int, str]] = {
    "RAYSPEC_PROP_SEED": (None, 0, "the base seed every property draws its cases from"),
    "RAYSPEC_PROP_CASES": (1, 60, "how many cases each property draws"),
}
#: The namespace :func:`read_environment` refuses unknown names in.
ENV_PREFIX = "RAYSPEC_PROP_"


def read_environment(env: Mapping[str, str]) -> tuple[int, int]:
    """``(base seed, cases per property)`` from ``env``; anything unusable is a loud error.

    Called once at import with ``os.environ``, and directly by the driver's own tests with a
    made-up mapping. Three ways to get it wrong, all of them silent before this existed:

    * ``RAYSPEC_PROP_CASES=0`` — every property then draws nothing, asserts nothing and passes.
      A whole generative suite reports green while checking exactly one thing by accident;
    * ``RAYSPEC_PROP_CASES=x`` — a value that is not an integer at all;
    * ``RAYSPEC_PROP_CASE=500`` — a name in this driver's namespace that it does not read. The
      run then quietly uses the default and the person who typed it believes they raised the
      case count, which is the same defect wearing a different hat.
    """
    known = ", ".join(sorted(ENV_RULES))
    for name in sorted(env):
        if name.startswith(ENV_PREFIX) and name not in ENV_RULES:
            raise BadEnvironment(
                f"{name}={env[name]!r}: this property driver owns {ENV_PREFIX}* and does not "
                f"read {name}, so setting it changes nothing. Known variables: {known}."
            )
    return _env_int("RAYSPEC_PROP_SEED", env), _env_int("RAYSPEC_PROP_CASES", env)


def _env_int(name: str, env: Mapping[str, str]) -> int:
    """One :data:`ENV_RULES` variable as an int; :class:`BadEnvironment` names it and its value."""
    minimum, default, what = ENV_RULES[name]
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadEnvironment(
            f"{name}={raw!r} is not an integer ({what}); unset it for the default of {default}"
        ) from None
    if minimum is not None and value < minimum:
        raise BadEnvironment(
            f"{name}={raw!r} is below {minimum} ({what}): a property that draws {value} case(s) "
            f"checks nothing and passes. Unset it for the default of {default}."
        )
    return value


#: Base seed for every property; override with ``RAYSPEC_PROP_SEED`` to draw different cases.
#: Cases per property; override with ``RAYSPEC_PROP_CASES`` (non-positive is refused above).
BASE_SEED, DEFAULT_CASES = read_environment(os.environ)
#: How many shrink candidates a falsified property may evaluate before it reports what it has.
DEFAULT_SHRINK_BUDGET = 300


class Falsified(AssertionError):
    """A property that a generated case falsified; the message names the seed and the case."""


class Discard(Exception):
    """Raised by a property body to say "this case is outside what the property covers".

    A precondition written as ``assert`` is indistinguishable from a falsified property, and the
    shrinker then does the worst possible thing with it: it "simplifies" a real counter-example
    into a case the property does not even apply to and reports *that*, with the precondition's
    own message as the failure. ``Discard`` keeps the two apart — the driver treats the case as
    not drawn, and shrinking walks past the candidate rather than adopting it.
    """


class NoCases(AssertionError):
    """The property checked nothing: it was asked for no cases, or every case was discarded."""


def seed_key(label: str, index: int, *, seed: int = BASE_SEED) -> str:
    """The reproducible key of one case — printed on failure, and the RNG's whole state."""
    return f"{label}#{seed}#{index}"


def rng_for(label: str, index: int, *, seed: int = BASE_SEED) -> random.Random:
    """The :class:`random.Random` of one case (seeded by :func:`seed_key`)."""
    return random.Random(seed_key(label, index, seed=seed))


def forall(
    label: str,
    gen: Callable[[random.Random], T],
    prop: Callable[[T], Any],
    *,
    cases: int = DEFAULT_CASES,
    seed: int = BASE_SEED,
    shrink: Callable[[T], Iterator[T]] | None = None,
    show: Callable[[T], str] = repr,
    budget: int = DEFAULT_SHRINK_BUDGET,
    examples: Sequence[T] = (),
) -> None:
    """Check ``examples``, then ``cases`` values from ``gen``; shrink and report the first failure.

    ``prop`` fails by raising (an ``assert`` is the normal way). The raised error is kept and
    re-reported with the minimal case ``shrink`` could reach. A :class:`Discard` says the case
    is outside the property's precondition; if every case discards, that is an error.

    ``examples`` are the cases the property must check whatever the RNG does — a row a
    generator reaches only when the seed is kind is not covered, it is covered *on this seed*,
    and the guard that says so passes for the same reason. Written into the test, checked first,
    shrunk and reported like any other case (the report says which example rather than a seed
    key, because no seed reproduces it).
    """
    _refuse_a_vacuous_run(label, cases)
    discarded = 0
    for drawn, (key, reproduce, value) in enumerate(_cases(label, gen, cases, seed, examples), 1):
        failure = _attempt(prop, value)
        if isinstance(failure, Discard):
            discarded += 1
            continue
        if failure is None:
            continue
        plan = _shrink_plan(value, failure, shrink, budget)
        minimal, failure = _drive(plan, lambda candidate: _attempt(prop, candidate))
        raise _report(label, key, reproduce, minimal, failure, show, drawn)
    _refuse_an_empty_run(label, discarded, cases + len(examples))


async def aforall(
    label: str,
    gen: Callable[[random.Random], T],
    prop: Callable[[T], Awaitable[Any]],
    *,
    cases: int = DEFAULT_CASES,
    seed: int = BASE_SEED,
    shrink: Callable[[T], Iterator[T]] | None = None,
    show: Callable[[T], str] = repr,
    budget: int = DEFAULT_SHRINK_BUDGET,
    examples: Sequence[T] = (),
) -> None:
    """:func:`forall` for an async property (the scheduler ones); same seeding and reporting."""
    _refuse_a_vacuous_run(label, cases)
    discarded = 0
    for drawn, (key, reproduce, value) in enumerate(_cases(label, gen, cases, seed, examples), 1):
        failure = await _aattempt(prop, value)
        if isinstance(failure, Discard):
            discarded += 1
            continue
        if failure is None:
            continue
        plan = _shrink_plan(value, failure, shrink, budget)
        minimal, failure = await _adrive(plan, lambda candidate: _aattempt(prop, candidate))
        raise _report(label, key, reproduce, minimal, failure, show, drawn)
    _refuse_an_empty_run(label, discarded, cases + len(examples))


# --------------------------------------------------------------------------------------------------
# driver internals
# --------------------------------------------------------------------------------------------------


def _cases(
    label: str,
    gen: Callable[[random.Random], T],
    cases: int,
    seed: int,
    examples: Sequence[T],
) -> Iterator[tuple[str, str, T]]:
    """``(key, how to reproduce it, value)`` for every case, the written examples first."""
    for index, value in enumerate(examples):
        yield f"{label}#example#{index}", f"examples[{index}] of property {label!r}", value
    for index in range(cases):
        yield (
            seed_key(label, index, seed=seed),
            f"rng_for({label!r}, {index}, seed={seed})",
            gen(rng_for(label, index, seed=seed)),
        )


def _refuse_a_vacuous_run(label: str, cases: int) -> None:
    """A property asked for no cases checks nothing and passes — the one failure a suite hides.

    The TOTAL rule, and deliberately at the driver rather than at the environment variable that
    is one way to reach it: ``cases`` is also arithmetic (``DEFAULT_CASES // 4``), a constant
    someone edited, a parameter a caller computed. Every route ends here.
    """
    if cases < 1:
        raise NoCases(
            f"property {label!r} was asked for cases={cases}: a property that draws no case "
            "checks nothing and passes, which is worse than not having it at all"
        )


def _refuse_an_empty_run(label: str, discarded: int, cases: int) -> None:
    """A property whose precondition rejected every drawn case checked nothing at all."""
    if discarded == cases:
        raise NoCases(
            f"property {label!r} discarded all {cases} case(s): the generator never produced "
            "a case the property applies to"
        )


def _attempt(prop: Callable[[T], Any], value: T) -> BaseException | None:
    """Run one case; the raised error, or ``None`` when the property held.

    A property signals failure by raising an ordinary exception — ``assert`` is the intended
    way — or a :class:`Discard`, which is returned like any other exception and which the
    callers then tell apart. ``Exception`` only, deliberately: a cancellation or a
    ``KeyboardInterrupt`` must never be mistaken for a falsified property (shrinking it would
    run the property hundreds more times). ``pytest.fail()`` / ``pytest.raises()`` derive from
    ``BaseException`` and therefore escape the driver unshrunk — use ``assert`` and
    :func:`raises`.
    """
    try:
        prop(value)
    except Exception as exc:
        return exc
    return None


async def _aattempt(prop: Callable[[T], Awaitable[Any]], value: T) -> BaseException | None:
    try:
        await prop(value)
    except Exception as exc:
        return exc
    return None


def _counter_example(failure: BaseException | None) -> BaseException | None:
    """``None`` for a case that held OR was discarded; the error for a real counter-example."""
    return None if isinstance(failure, Discard) else failure


#: A shrink plan yields candidates and is told (via ``send``) whether each one still fails.
ShrinkPlan = Generator[T, "BaseException | None", "tuple[T, BaseException]"]


def _shrink_plan(
    value: T,
    failure: BaseException,
    shrink: Callable[[T], Iterator[T]] | None,
    budget: int,
) -> ShrinkPlan[T]:
    """Greedy shrinking: keep the first simpler candidate that still fails, then start over.

    Simplification is the generator's business (``shrink`` yields candidates simplest-first);
    the plan only decides what to keep. ``budget`` bounds the candidates evaluated, so a slow
    property (a whole workflow run) cannot turn one failure into an unbounded suite.
    """
    best, best_failure = value, failure
    tried = 0
    if shrink is not None:
        improved = True
        while improved and tried < budget:
            improved = False
            for candidate in shrink(best):
                if tried >= budget:
                    break
                tried += 1
                result = yield candidate
                if result is not None:
                    best, best_failure = candidate, result
                    improved = True
                    break
    return best, best_failure


def _drive(
    plan: ShrinkPlan[T], attempt: Callable[[T], BaseException | None]
) -> tuple[T, BaseException]:
    try:
        candidate = next(plan)
        while True:
            candidate = plan.send(_counter_example(attempt(candidate)))
    except StopIteration as stop:
        return stop.value


async def _adrive(
    plan: ShrinkPlan[T], attempt: Callable[[T], Awaitable[BaseException | None]]
) -> tuple[T, BaseException]:
    try:
        candidate = next(plan)
        while True:
            candidate = plan.send(_counter_example(await attempt(candidate)))
    except StopIteration as stop:
        return stop.value


class Raised(Generic[E]):
    """The exception a :func:`raises` block caught, readable after the block."""

    def __init__(self) -> None:
        self.caught: tuple[E, ...] = ()

    @property
    def value(self) -> E:
        """The caught exception; defined once the block has exited."""
        if not self.caught:
            raise AssertionError("nothing was raised")
        return self.caught[0]


@contextlib.contextmanager
def raises(expected: type[E]) -> Iterator[Raised[E]]:
    """``pytest.raises`` for a property body: a miss is an ordinary ``AssertionError``.

    ``pytest.raises`` reports a miss as ``Failed``, which derives from ``BaseException`` and
    therefore travels straight past :func:`_attempt` — the property fails without a seed key and
    without a minimal case, which is exactly what this driver exists to prevent.
    """
    box: Raised[E] = Raised()
    try:
        yield box
    except expected as exc:
        box.caught = (exc,)
        return
    raise AssertionError(f"expected {expected.__name__} to be raised, nothing was")


def _report(
    label: str,
    key: str,
    reproduce: str,
    minimal: T,
    failure: BaseException,
    show: Callable[[T], str],
    drawn: int,
) -> Falsified:
    return Falsified(
        f"property {label!r} falsified after {drawn} case(s)\n"
        f"  seed key:     {key}\n"
        f"  minimal case: {show(minimal)}\n"
        f"  failure:      {type(failure).__name__}: {failure}\n"
        f"  reproduce:    {reproduce}"
    )


# --------------------------------------------------------------------------------------------------
# value generators
# --------------------------------------------------------------------------------------------------

#: Fragments a generated string is assembled from. Everything a shell body, a Python literal or
#: a Jinja template could plausibly choke on: quoting, expansion, command substitution, comment
#: and template delimiters, control characters, combining marks, astral-plane code points, and
#: the substitution rayspec itself performs (``${RAYSPEC_V1}``) so a value that looks like a slot
#: is generated too. NUL is deliberately absent — it cannot travel in a process environment at
#: all, so it is not a round-trip question (see ``test_templating_slots.py``).
#:
#: The command substitutions here are deliberately INERT (``$(id)``, ``` `id` ```): these
#: fragments are spliced into values fed to a real bash on every run, and the one moment a
#: destructive payload could fire is the moment the property exists to detect — a regression to
#: splicing. Destructive intent belongs in the canary property, which wraps every value in
#: ``$(touch <canary>)`` and asserts the canary was never created, not in the payload.
FRAGMENTS: tuple[str, ...] = (
    "a",
    "z",
    "0",
    " ",
    "\t",
    "\n",
    "\r",
    "'",
    '"',
    "\\",
    "\\n",
    "$",
    "`",
    "${RAYSPEC_V1}",
    "${RAYSPEC_V2}",
    "$(id)",
    "`id`",
    "$HOME",
    "${#x}",
    "%s",
    "%%",
    ";",
    "|",
    "&&",
    "<<EOF",
    "#",
    "{{ inputs.evil }}",
    "{% raw %}",
    "{{#",
    "#}}",
    "${{",
    "--",
    "-n",
    "\x1b[31m",  # an ANSI escape
    "\x07",
    "\u00e9",  # precomposed e-acute ...
    "e\u0301",  # ... and the same letter decomposed: NFC/NFD must survive as written
    "\u00fc",
    "\u6f22\u5b57",
    "\U0001f600",  # astral plane: one code point, two UTF-16 units
    "\u200b",  # zero-width space
    "\u00df",
    "\u0130",  # dotted capital I: lowercases to two code points
)


def text(rng: random.Random, *, max_parts: int = 6) -> str:
    """A short string assembled from :data:`FRAGMENTS` (possibly empty)."""
    return "".join(rng.choice(FRAGMENTS) for _ in range(rng.randint(0, max_parts)))


def shrink_text(value: str) -> Iterator[str]:
    """Simpler strings: the empty one, halves, then each single-character deletion."""
    if not value:
        return
    yield ""
    if len(value) > 2:
        half = len(value) // 2
        yield value[:half]
        yield value[half:]
    for i in range(len(value)):
        yield value[:i] + value[i + 1 :]


def json_value(rng: random.Random, *, depth: int = 2) -> Any:
    """A JSON-like value: str / int / float / bool / None / list / dict with string keys."""
    choices = ["str", "int", "float", "bool", "none"]
    if depth > 0:
        choices += ["list", "dict"]
    kind = rng.choice(choices)
    if kind == "str":
        return text(rng, max_parts=4)
    if kind == "int":
        return rng.choice([0, 1, -1, 42, 2**53, -(2**53), 10**20])
    if kind == "float":
        return rng.choice([0.0, -0.0, 1.5, -2.25, 1e300, 1e-300, 3.141592653589793])
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "none":
        return None
    if kind == "list":
        return [json_value(rng, depth=depth - 1) for _ in range(rng.randint(0, 3))]
    return {
        text(rng, max_parts=2) or f"k{i}": json_value(rng, depth=depth - 1)
        for i in range(rng.randint(0, 3))
    }


def shrink_json(value: Any) -> Iterator[Any]:
    """Simpler JSON values: towards ``None``, shorter containers, simpler strings."""
    if value is None:
        return
    yield None
    if isinstance(value, str):
        yield from shrink_text(value)
    elif isinstance(value, list):
        if value:
            yield []
        for i in range(len(value)):
            yield value[:i] + value[i + 1 :]
        for i, item in enumerate(value):
            for smaller in _first(shrink_json(item), 3):
                yield [*value[:i], smaller, *value[i + 1 :]]
    elif isinstance(value, dict):
        if value:
            yield {}
        for key in list(value):
            yield {k: v for k, v in value.items() if k != key}
    elif isinstance(value, bool):
        if value:
            yield False
    elif isinstance(value, int | float) and value != 0:
        yield 0


def shrink_seq(values: Sequence[T]) -> Iterator[list[T]]:
    """Simpler sequences: halves, then each single-element deletion."""
    items = list(values)
    if not items:
        return
    if len(items) > 2:
        half = len(items) // 2
        yield items[:half]
        yield items[half:]
    for i in range(len(items)):
        yield items[:i] + items[i + 1 :]


def _first(it: Iterator[T], n: int) -> Iterator[T]:
    for i, item in enumerate(it):
        if i >= n:
            return
        yield item


__all__ = [
    "BASE_SEED",
    "DEFAULT_CASES",
    "DEFAULT_SHRINK_BUDGET",
    "ENV_PREFIX",
    "ENV_RULES",
    "FRAGMENTS",
    "BadEnvironment",
    "Discard",
    "Falsified",
    "NoCases",
    "Raised",
    "aforall",
    "forall",
    "json_value",
    "raises",
    "read_environment",
    "rng_for",
    "seed_key",
    "shrink_json",
    "shrink_seq",
    "shrink_text",
    "text",
]
