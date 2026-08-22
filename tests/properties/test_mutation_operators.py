# SPDX-License-Identifier: Apache-2.0
"""The mutation harness itself — the only part of it pytest runs.

The mutation *report* is produced by hand (it takes an hour and CI is not gated on a score), but
the machinery that produces it has to be trustworthy, and three of its promises are cheap to
check on every run:

* the site numbering is **stable** — a survivor is quoted by ``file:line`` and index, and an
  index that means something different tomorrow makes the report a lie;
* a mutant is **semantically different** — an "operator" that produced equivalent code would
  report survivors nobody can act on;
* the round trip through ``ast.unparse`` is **behaviour-preserving** for every target, which is
  what makes a kill attributable to the mutation rather than to the round trip;
* and nothing in :mod:`tests.properties.mutation.mutate` writes inside the working tree.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rayspec.engine.graph import JoinDecision
from rayspec.schema import StepStatus

from .mutation.mutate import (
    MUTANT_STRING,
    OPERATORS,
    REPO,
    TARGETS,
    main,
    mutate,
    null_mutant,
    sites,
)

SAMPLE = '''
"""A module docstring — never a mutation site."""

from dataclasses import dataclass
from typing import Literal

__all__ = ["decide"]

Mode = Literal["fast", "slow"]


@dataclass(frozen=True, slots=True)
class Rules:
    """A docstring — never a mutation site."""

    strict: bool = True


def decide(mode: Mode, flags: list[str]) -> bool:
    """A docstring — never a mutation site."""
    if mode == "fast" and "x" in flags:
        return True
    if not flags:
        return False
    return any(f.startswith("y") for f in flags)
'''


def test_the_operators_are_all_reachable() -> None:
    """Every advertised operator finds at least one site in the sample."""
    found = {site.operator for site in sites(SAMPLE)}
    assert found == set(OPERATORS), f"missing {set(OPERATORS) - found}"


def test_docstrings_annotations_and_dunder_all_are_not_sites() -> None:
    """Mutating them could never be killed — they would bury the survivors that matter."""
    mutated = {site.before for site in sites(SAMPLE) if site.operator == "str"}
    assert mutated == {"'fast'", "'x'", "'y'"}, mutated


def test_decorator_flags_are_not_sites() -> None:
    """``@dataclass(frozen=True, slots=True)`` is hygiene, not a decision.

    Before this exclusion every frozen dataclass in a target contributed two survivors that
    said nothing, which is how a mutation score stops being read.
    """
    lines = {site.line for site in sites(SAMPLE) if site.operator == "bool"}
    decorator = SAMPLE.splitlines().index("@dataclass(frozen=True, slots=True)") + 1
    assert decorator not in lines, lines
    assert lines, "the field default is still a site"


def test_the_numbering_is_stable() -> None:
    """Two enumerations of one source agree — the report quotes indexes."""
    first = sites(SAMPLE)
    second = sites(SAMPLE)
    assert [(s.index, s.line, s.operator, s.before) for s in first] == [
        (s.index, s.line, s.operator, s.before) for s in second
    ]
    assert [s.index for s in first] == list(range(len(first)))


def test_every_mutant_parses_and_differs() -> None:
    """A mutant that does not compile is not a mutant; one that is identical is not either."""
    baseline = ast.dump(ast.parse(null_mutant(SAMPLE)))
    for site in sites(SAMPLE):
        source = mutate(SAMPLE, site.index)
        assert ast.dump(ast.parse(source)) != baseline, f"site {site.index} changed nothing"


def test_a_mutated_join_table_really_decides_differently() -> None:
    """End to end on the real module: the ``join: always`` row, mutated, stops running.

    This is the premise of the whole report — if the harness produced equivalent code, every
    "killed" would be an accident and every "survivor" meaningless.
    """
    source = TARGETS["graph"].path.read_text(encoding="utf-8")
    target = next(
        site
        for site in sites(source)
        if site.operator == "compare"
        and site.before == "=="
        and "always" in _line(source, site.line)
    )
    # ``__name__`` names the real module: ``dataclasses`` resolves a field annotation through
    # ``sys.modules[cls.__module__]`` and a name nothing has imported would break the exec.
    namespace: dict[str, Any] = {"__name__": "rayspec.engine.graph"}
    exec(compile(mutate(source, target.index), "<mutant>", "exec"), namespace)
    join_decision: Callable[..., JoinDecision] = namespace["join_decision"]
    step = _always_step()
    failed = _record(StepStatus.FAILED)
    assert join_decision(step, [failed], draining=False) != JoinDecision.go()


def test_the_round_trip_preserves_every_target(tmp_path: Path) -> None:
    """``ast.unparse(ast.parse(src))`` re-parses to the same tree for every module we mutate."""
    for target in TARGETS.values():
        source = target.path.read_text(encoding="utf-8")
        assert ast.dump(ast.parse(null_mutant(source))) == ast.dump(ast.parse(source)), target.name


def test_the_targets_exist_and_are_inside_the_repository() -> None:
    """A renamed module must break this test, not silently drop a target from the report."""
    for target in TARGETS.values():
        assert target.path.is_file(), target.module
        assert target.path.is_relative_to(REPO / "src")


def test_mutating_is_a_pure_function_of_the_source() -> None:
    """``mutate`` never touches disk: the module it names is byte-identical afterwards."""
    target = TARGETS["graph"]
    before = target.path.read_bytes()
    for site in sites(before.decode("utf-8"))[:5]:
        mutate(before.decode("utf-8"), site.index)
    assert target.path.read_bytes() == before


def test_a_mutated_string_is_recognisable() -> None:
    """A mutated string is one nothing in the codebase expects, so a kill is unambiguous."""
    source = mutate(SAMPLE, next(s.index for s in sites(SAMPLE) if s.operator == "str"))
    assert MUTANT_STRING in source


def test_narrowing_the_operators_narrows_the_numbering_consistently() -> None:
    """A run may mutate a subset of operators; ``sites`` and ``mutate`` must agree on the set.

    The indexes of a narrowed run mean nothing to a run that used a different subset, which is
    why the subset is a parameter of both rather than a global somebody could forget to pass.
    """
    only = frozenset({"compare"})
    narrowed = sites(SAMPLE, only)
    assert narrowed and {site.operator for site in narrowed} == only
    assert [site.index for site in narrowed] == list(range(len(narrowed)))
    for site in narrowed:
        source = mutate(SAMPLE, site.index, only)
        assert MUTANT_STRING not in source, "a narrowed run must not reach another operator"
        assert ast.dump(ast.parse(source)) != ast.dump(ast.parse(null_mutant(SAMPLE)))


def test_the_cli_refuses_indexes_without_one_target() -> None:
    """Site indexes are per module; naming them across targets would mutate the wrong lines."""
    with pytest.raises(SystemExit):
        main(["--index", "0", "--no-confirm"])


def test_an_unknown_index_is_refused() -> None:
    """An index past the last site is a bug in the caller, not a silently skipped mutant."""
    with pytest.raises(IndexError):
        mutate(SAMPLE, 10_000)


def _line(source: str, number: int) -> str:
    return source.splitlines()[number - 1]


class _Step:
    join = "always"


class _Record:
    def __init__(self, status: StepStatus) -> None:
        self.status = status
        self.tolerated = False


def _always_step() -> _Step:
    return _Step()


def _record(status: StepStatus) -> _Record:
    return _Record(status)
