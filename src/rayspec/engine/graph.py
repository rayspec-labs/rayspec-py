# SPDX-License-Identifier: Apache-2.0
"""Step graphs and the ``join`` decision.

Module boundary: pure data. A :class:`StepGraph` is one sibling list (the root steps or a
composite's body) indexed by id with ``needs``/``dependents``/``roots``; :func:`join_decision`
implements the plan's truth table over the terminal outcomes of a step's ``needs``. Nothing
here touches templating, stores or providers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from rayspec.engine.errors import GraphError
from rayspec.schema import StepBase, StepModel, StepStatus


class HasOutcome(Protocol):
    """What :func:`join_decision` needs from a terminal record (``StepRecord`` fits)."""

    @property
    def status(self) -> StepStatus: ...

    @property
    def tolerated(self) -> bool: ...


#: Terminal statuses that count as "failed (untolerated)" in the join table.
FAILED_LIKE: frozenset[StepStatus] = frozenset(
    {StepStatus.FAILED, StepStatus.INTERRUPTED, StepStatus.REJECTED}
)


@dataclass(frozen=True, slots=True)
class JoinDecision:
    """``run``, or skip with ``skip_reason`` (upstream_skipped / upstream_failed / run_failed)."""

    run: bool
    skip_reason: str | None = None

    @classmethod
    def go(cls) -> JoinDecision:
        return cls(True, None)

    @classmethod
    def skip(cls, reason: str) -> JoinDecision:
        return cls(False, reason)


def classify(outcome: HasOutcome) -> str:
    """``succeeded`` (incl. tolerated failures), ``skipped`` or ``failed`` for the join table."""
    status = outcome.status
    if status is StepStatus.SUCCEEDED:
        return "succeeded"
    if status is StepStatus.FAILED and outcome.tolerated:
        return "succeeded"
    if status is StepStatus.SKIPPED:
        return "skipped"
    if status in FAILED_LIKE:
        return "failed"
    raise ValueError(f"join_decision needs terminal outcomes, got {status!r}")


def join_decision(
    step: StepBase, outcomes_of_needs: Iterable[HasOutcome], *, draining: bool
) -> JoinDecision:
    """The plan's join truth table (evaluated only once ALL ``needs`` are terminal).

    ==================================  ======  ======  ========
    needs outcome                        all     any     always
    ==================================  ======  ======  ========
    all succeeded (tolerated = ok)       run     run     run
    ≥1 skipped, rest ok, none failed     skip    run     run
    all skipped                          skip    skip    run
    ≥1 failed (untolerated)              skip    skip    run
    run draining / cancelled             skip    skip    run
    ==================================  ======  ======  ========
    """
    classes = [classify(o) for o in outcomes_of_needs]
    join = step.join
    if join == "always":
        return JoinDecision.go()
    if any(c == "failed" for c in classes):
        return JoinDecision.skip("upstream_failed")
    if classes and all(c == "skipped" for c in classes):
        return JoinDecision.skip("upstream_skipped")
    if join == "all" and any(c == "skipped" for c in classes):
        return JoinDecision.skip("upstream_skipped")
    if draining:
        return JoinDecision.skip("run_failed")
    return JoinDecision.go()


@dataclass(frozen=True, slots=True)
class StepGraph:
    """One sibling list as a DAG: ``needs`` per id, ``dependents`` per id, ``roots``."""

    steps: tuple[StepModel, ...]
    by_id: Mapping[str, StepModel]
    needs: Mapping[str, tuple[str, ...]]
    dependents: Mapping[str, tuple[str, ...]]
    roots: tuple[str, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.steps)

    def step(self, step_id: str) -> StepModel:
        return self.by_id[step_id]

    @classmethod
    def from_steps(cls, steps: Sequence[StepModel]) -> StepGraph:
        """Build and validate: unique ids, ``needs`` name siblings only, no cycles."""
        by_id: dict[str, StepModel] = {}
        for step in steps:
            if step.id in by_id:
                raise GraphError(f"duplicate step id {step.id!r} in one sibling list")
            by_id[step.id] = step
        needs: dict[str, tuple[str, ...]] = {}
        dependents: dict[str, list[str]] = {sid: [] for sid in by_id}
        for step in steps:
            for need in step.needs:
                if need not in by_id:
                    raise GraphError(
                        f"step {step.id!r} needs unknown sibling {need!r} "
                        "(needs may only name steps of the same sibling list)"
                    )
                if need == step.id:
                    raise GraphError(f"step {step.id!r} needs itself")
            needs[step.id] = tuple(dict.fromkeys(step.needs))
            for need in needs[step.id]:
                dependents[need].append(step.id)
        _check_acyclic(needs)
        roots = tuple(s.id for s in steps if not needs[s.id])
        return cls(
            steps=tuple(steps),
            by_id=by_id,
            needs=needs,
            dependents={k: tuple(v) for k, v in dependents.items()},
            roots=roots,
        )


def _check_acyclic(needs: Mapping[str, tuple[str, ...]]) -> None:
    state: dict[str, int] = {}

    def visit(sid: str, stack: list[str]) -> None:
        mark = state.get(sid, 0)
        if mark == 1:
            cycle = [*stack[stack.index(sid) :], sid]
            raise GraphError(f"needs cycle: {' -> '.join(cycle)}")
        if mark == 2:
            return
        state[sid] = 1
        stack.append(sid)
        for need in needs[sid]:
            visit(need, stack)
        stack.pop()
        state[sid] = 2

    for sid in needs:
        visit(sid, [])


__all__ = ["FAILED_LIKE", "HasOutcome", "JoinDecision", "StepGraph", "classify", "join_decision"]
