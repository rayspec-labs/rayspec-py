# SPDX-License-Identifier: Apache-2.0
"""Scheduler invariants over random DAGs — inside ``loop:``, ``each:`` and ``include:`` bodies.

``tests/engine/test_join_table.py`` generates FLAT graphs. That is where the join table was
first checked, and it is not where the interesting bugs live: every composite runs its body
through the same :func:`~rayspec.engine.scheduler.run_graph`, with its own failure policy, its
own teardown and its own wind-down, and a defect that only bites two levels down (a body whose
``join: always`` cleanup is skipped, a composite recorded ``succeeded`` over a failed body) is
invisible to a flat generator.

So the cases here are *nested*: a root DAG whose steps may be loops, fan-outs or includes whose
bodies are themselves DAGs. Every recorded sibling list — root, ``L[2]/…``, ``E[0]/…``,
``I/…`` — is checked against the same four invariants:

* **decided** — the root list always settles every step, teardown included (that is what the
  wind-down is for);
* **the join table** — the plan's truth table, per sibling list, over the outcomes actually
  recorded;
* **composite derivation** — a composite is ``failed`` exactly when its body holds an
  untolerated failure, and ``succeeded`` otherwise;
* **teardown discipline** — draining never interrupts anything, and a ``stop:`` bubbles exactly
  when a ``stop:`` step ran.

A failure here is a defect report: the seed and the minimal workflow are printed, and the
generated YAML of the minimal case is in the message.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TypeVar

import anyio
import pytest
from anyio import to_thread

# ``GraphHarness`` is a type, not a fixture: it comes straight from the engine harness that
# ``tests/properties/conftest.py`` re-exports the fixtures of, so the two cannot drift apart.
from engine.conftest import GraphHarness
from rayspec.engine.context import RunOptions
from rayspec.engine.errors import RunStopped
from rayspec.engine.scheduler import run_graph
from rayspec.schema import StepStatus
from rayspec.store.model import StepRecord

from .conftest import Harness, make_graph_harness
from .generate import DEFAULT_CASES, Raised, aforall, raises, shrink_seq

pytestmark = pytest.mark.anyio

T = TypeVar("T")

#: Statuses a step can only have because it was actually dispatched.
RAN = frozenset({StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.INTERRUPTED})
#: Terminal statuses the join table counts as a failure — restated here, see :func:`classify_row`.
FAILED_STATUSES = frozenset({StepStatus.FAILED, StepStatus.INTERRUPTED, StepStatus.REJECTED})
#: ``skip_reason`` values that mean "the graph ended", not "this step's needs decided it".
DRAIN_REASONS = frozenset({"run_failed", "stopped"})
#: The marker :func:`gen_steps` expands to ``block:<the step's own id>`` — see :data:`BODIES`.
GATE = "block"
#: Leaf bodies the fake executor understands. Two of the five are GATES: a ``block:<id>`` step
#: enters its body and stays there until the test releases it, which is what puts siblings in
#: flight concurrently — the only state in which a teardown has anything to tear down.
#:
#: The gates replaced the two sleeps this list used to carry. A sleep only *guesses* at that
#: state, and it guessed against the engine's own per-step bookkeeping: two ``anyio.to_thread``
#: writes of ``run.json``, each a temp file, an fsync and a rename, of the same order as the
#: sleeps themselves. Whether a sibling was still running when the teardown began was therefore
#: the machine's business rather than the test's, and ``seen["interrupted"]`` — the count that
#: says this property looked at a teardown with something to tear down — moved from run to run.
#: A gate makes it a fact: the step is in its body until :func:`run_case` says otherwise.
BODIES = ("ok", "ok", "fail", GATE, GATE)
#: The four failure policies a generated case is run under.
MODES = ("drain", "fail_fast", "continue", "stop")
#: Generated cases per property and mode — a quarter of the usual count, because every case
#: here runs a whole graph. ``RAYSPEC_PROP_CASES`` only ever RAISES it: the floor of 12 is what
#: the coverage guards below need in order to mean anything, and a value below it would leave
#: them asserting on a handful of cases. It can never reach zero either way — ``DEFAULT_CASES``
#: is refused below 1 at import, and :func:`~.generate.forall` refuses a non-positive count
#: whatever arithmetic produced it.
CASES = max(12, DEFAULT_CASES // 4)


# --------------------------------------------------------------------------------------------------
# the generated workflow
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gen:
    """One generated step. ``children`` is the body of a composite (empty for a leaf)."""

    id: str
    kind: str  # leaf | stop | loop | each | include
    needs: tuple[str, ...] = ()
    join: str = "all"
    body: str = "ok"
    allow_failure: bool = False
    when_false: bool = False
    children: tuple[Gen, ...] = ()
    iterations: int = 1
    items: int = 2
    on_failure: str = "fail"

    def yaml(self, indent: int) -> list[str]:
        """This step as YAML lines, indented to ``indent`` (composites recurse)."""
        pad = " " * indent
        key = " " * (indent + 2)
        lines = [f"{pad}- id: {self.id}"]
        if self.needs:
            lines.append(f"{key}needs: [{', '.join(self.needs)}]")
        if self.join != "all":
            lines.append(f"{key}join: {self.join}")
        if self.allow_failure:
            lines.append(f"{key}allow_failure: true")
        if self.when_false:
            lines.append(f'{key}when: "false"')
        if self.kind == "leaf":
            lines.append(f"{key}shell: {self.body!r}")
        elif self.kind == "stop":
            lines.append(f"{key}stop: {{status: cancelled, reason: generated}}")
        elif self.kind == "loop":
            lines.append(f"{key}loop:")
            lines.append(f"{key}  max_iterations: {self.iterations}")
            lines.append(f"{key}  steps:")
            lines.extend(body_yaml(self.children, indent + 6))
        elif self.kind == "each":
            lines.append(f'{key}each: "{list(range(self.items))}"')
            lines.append(f"{key}on_failure: {self.on_failure}")
            lines.append(f"{key}steps:")
            lines.extend(body_yaml(self.children, indent + 4))
        elif self.kind == "include":
            lines.append(f"{key}include: {include_name(self.id)}")
        return lines


def body_yaml(steps: tuple[Gen, ...], indent: int) -> list[str]:
    """A sibling list as YAML lines."""
    return [line for step in steps for line in step.yaml(indent)]


def include_name(step_id: str) -> str:
    """The workflow file an ``include:`` step of this id points at."""
    return f"inc_{step_id}"


@dataclass(frozen=True)
class Case:
    """A whole generated case: the root sibling list plus the mode it is run under."""

    mode: str
    steps: tuple[Gen, ...]

    def workflow_yaml(self) -> str:
        """The root workflow document for this case."""
        defaults = "defaults:\n  on_step_failure: continue\n" if self.mode == "continue" else ""
        return f"rayspec: 1\nname: t\n{defaults}steps:\n" + "\n".join(body_yaml(self.steps, 2))

    def includes(self) -> dict[str, str]:
        """``{file stem: yaml}`` for every ``include:`` step at any depth."""
        out: dict[str, str] = {}
        for step in walk(self.steps):
            if step.kind == "include":
                name = include_name(step.id)
                out[name] = f"rayspec: 1\nname: {name}\nsteps:\n" + "\n".join(
                    body_yaml(step.children, 2)
                )
        return out

    def __str__(self) -> str:
        parts = [f"mode={self.mode}", self.workflow_yaml()]
        parts += [f"# {name}.yaml\n{text}" for name, text in self.includes().items()]
        return "\n".join(parts)


def walk(steps: tuple[Gen, ...]):
    """Every generated step, composites' bodies included."""
    for step in steps:
        yield step
        yield from walk(step.children)


def by_id(steps: tuple[Gen, ...]) -> dict[str, Gen]:
    """Every generated step by id — ids are unique across a whole case."""
    return {step.id: step for step in walk(steps)}


def gate_of(step: Gen) -> str | None:
    """The gate a leaf's body parks on (``block:<id>`` → ``<id>``), or ``None`` for a body that
    runs straight through."""
    if step.kind == "leaf" and step.body.startswith(f"{GATE}:"):
        return step.body.split(":", 1)[1]
    return None


def gates_of(steps: tuple[Gen, ...]) -> frozenset[str]:
    """Every gate name in a sibling list, composites' bodies included.

    The name is the step's own id, so the gate a running step is parked on is the last segment
    of its record path — ``s1[0]/s4`` is parked on ``s4``. Ids are unique across a whole case,
    which is what makes that reading unambiguous.
    """
    return frozenset(name for step in walk(steps) if (name := gate_of(step)) is not None)


def owner_of(case: Case, path: str) -> Gen | None:
    """The generated step a record path names: ``s1[0]/s4`` → ``s4``, ``s3[2]`` → ``s3``.

    Ids are unique across a whole case, which is what makes reading the last segment enough —
    a body instance's index (``[2]``) is run state, not part of the step's identity.
    """
    return by_id(case.steps).get(path.rsplit("/", 1)[-1].split("[")[0])


# --------------------------------------------------------------------------------------------------
# generation and shrinking
# --------------------------------------------------------------------------------------------------


def gen_steps(rng: random.Random, counter: list[int], *, depth: int, size: int) -> tuple[Gen, ...]:
    """A sibling list of ``size`` steps in shuffled (non-topological) declaration order.

    ``when:`` is drawn independently of ``needs``, which is what makes the two shapes the flat
    generator could not reach appear here: a step that is SKIPPED while its siblings succeed
    (so a downstream ``join: any`` sees a mixed ``needs`` — the one row where ``any`` differs
    from ``all``, and the entire reason ``join: any`` exists), and a ``join: always`` step
    carrying a ``when:``, which is the only way into the teardown's own ``when:`` branch.
    """
    steps: list[Gen] = []
    for _ in range(size):
        counter[0] += 1
        sid = f"s{counter[0]}"
        earlier = [s.id for s in steps]
        needs = tuple(sorted(rng.sample(earlier, k=rng.randint(0, min(2, len(earlier))))))
        kinds = ["leaf", "leaf", "leaf"]
        if depth > 0:
            kinds += ["loop", "each", "include"]
        kind = rng.choice(kinds)
        children = (
            gen_steps(rng, counter, depth=depth - 1, size=rng.randint(1, 3))
            if kind in {"loop", "each", "include"}
            else ()
        )
        body = rng.choice(BODIES)
        steps.append(
            Gen(
                id=sid,
                kind=kind,
                needs=needs,
                join=rng.choice(["all", "any", "any", "always"]) if needs else "all",
                body=f"{GATE}:{sid}" if body == GATE else body,
                allow_failure=rng.random() < 0.15,
                when_false=rng.random() < 0.2,
                children=children,
                iterations=rng.randint(1, 2),
                items=rng.choice([0, 1, 2, 2]),
                on_failure=rng.choice(["fail", "fail", "continue"]),
            )
        )
    rng.shuffle(steps)
    return tuple(steps)


def case_for(mode: str):
    """A generator of :class:`Case` values for one failure policy."""

    def gen(rng: random.Random) -> Case:
        steps = gen_steps(rng, [0], depth=2, size=rng.randint(3, 6))
        if mode == "stop":
            steps = with_a_stop(rng, steps)
        return Case(mode=mode, steps=steps)

    return gen


def mixed_join_row_case(mode: str) -> Case:
    """The ``join: any`` row the RNG only reaches when the seed is kind — written down instead.

    ``any`` differs from ``all`` in exactly one row of the table: a step whose needs hold BOTH a
    skipped and a succeeded outcome runs under ``any`` and skips under ``all``. That row is the
    entire reason ``join: any`` exists, and :func:`gen_steps` reached it on 4 of 10 seeds — so
    the guard that says the row was checked was itself passing by luck, and on the other 6 seeds
    the table's most interesting row went unchecked while the suite reported green.

    Three root steps reach it and nothing else can take them away: ``mix_skipped`` carries
    ``when: false`` (skipped however the run ends), ``mix_ok`` has no needs and cannot fail, and
    ``mix_any`` needs both under ``join: any``. Nothing in the case fails or stops, so no mode's
    teardown ever starts and the row is decided the same way under all four.
    """
    return Case(
        mode=mode,
        steps=(
            Gen(id="mix_skipped", kind="leaf", when_false=True),
            Gen(id="mix_ok", kind="leaf", body="ok"),
            Gen(id="mix_any", kind="leaf", needs=("mix_skipped", "mix_ok"), join="any"),
        ),
    )


def torn_down_cases(mode: str) -> tuple[Case, Case]:
    """The two shapes in which a teardown has something to tear down — written down, not hoped for.

    ``seen["interrupted"]`` is the count that says this property looked at a teardown with a step
    still inside it, and :func:`gen_steps` reaches that shape only when the seed is kind. A
    generated root list has one to three entry points and everything else hangs off them, so by
    the time the step that fails gets to fail it is usually the only thing the graph had left to
    do. Measured over the fifteen drawn cases of a ``fail_fast`` run: one of them. A guard resting
    on that passes for the same reason the row it guards is missing.

    So both shapes are stated. ``held`` parks in its body and only the teardown ends it — in the
    first case the trip is ready alongside it, in the second it is a stage further on, which is
    the shape a failure deep in a graph and a cleanup at its root have. Under ``drain`` and
    ``continue`` nothing cancels: ``held`` is then let go once the graph has nothing else to do,
    which is the drain those modes' own half of the invariant is about.

    The trip itself is whatever tears a list down in this mode — a ``stop:`` under ``stop``, an
    untolerated failure under the other three.
    """

    def trip(step_id: str, needs: tuple[str, ...] = ()) -> Gen:
        if mode == "stop":
            return Gen(id=step_id, kind="stop", needs=needs)
        return Gen(id=step_id, kind="leaf", body="fail", needs=needs)

    together = Case(
        mode=mode,
        steps=(Gen(id="held", kind="leaf", body=f"{GATE}:held"), trip("trip")),
    )
    a_stage_apart = Case(
        mode=mode,
        steps=(
            Gen(id="held_across", kind="leaf", body=f"{GATE}:held_across"),
            Gen(id="first", kind="leaf", body="ok"),
            trip("trip_across", needs=("first",)),
        ),
    )
    return together, a_stage_apart


def with_a_stop(rng: random.Random, steps: tuple[Gen, ...]) -> tuple[Gen, ...]:
    """Turn one generated leaf — at any depth — into a ``stop:``, and give it a cleanup sibling.

    A ``stop:`` inside a body is the interesting placement: the signal has to travel out of the
    composite and tear down the list ABOVE it, which is the path a flat generator never reaches.

    The cleanup sibling is a ``join: always`` step carrying ``when: "false"`` that needs the
    ``stop:``. Nothing else can decide it — its need is only terminal once the stop has fired,
    and the stop tears the list down in the same breath — so it is always the WIND-DOWN that
    evaluates its ``when:``, which is a branch of its own and not the ready-set check it is
    copied from.
    """
    leaves = [s.id for s in walk(steps) if s.kind == "leaf" and not s.when_false]
    if not leaves:
        return steps
    target = rng.choice(leaves)
    cleanup = Gen(
        id=f"{target}_cleanup", kind="leaf", needs=(target,), join="always", when_false=True
    )

    def rewrite(items: tuple[Gen, ...]) -> tuple[Gen, ...]:
        out: list[Gen] = []
        for s in items:
            if s.id == target:
                out.append(replace(s, kind="stop", children=()))
                out.append(cleanup)
            else:
                out.append(replace(s, children=rewrite(s.children)))
        return tuple(out)

    return rewrite(steps)


def shrink_case(case: Case):
    """Simpler cases: fewer root steps, then composites collapsed to leaves.

    Dropping a step also drops every ``needs`` that named it, so a shrunk case still loads. A
    list is never emptied: ``steps:`` is required, and an empty one is a schema error rather
    than a simpler counter-example.
    """
    for smaller in shrink_seq(case.steps):
        if smaller:
            yield replace(case, steps=repair(tuple(smaller)))
    for index, step in enumerate(case.steps):
        if step.children:
            flat = replace(step, kind="leaf", children=(), body="ok")
            yield replace(case, steps=(*case.steps[:index], flat, *case.steps[index + 1 :]))
        for smaller in shrink_seq(step.children):
            if not smaller:
                continue
            trimmed = replace(step, children=repair(tuple(smaller)))
            yield replace(case, steps=(*case.steps[:index], trimmed, *case.steps[index + 1 :]))


def repair(steps: tuple[Gen, ...]) -> tuple[Gen, ...]:
    """Drop ``needs`` that no longer name a sibling (the shrinker removed the step).

    A ``join`` left without ``needs`` goes with them: it has no effect, and the loader says so
    as a warning that a shrunk case has no reason to carry.
    """
    known = {s.id for s in steps}
    kept = tuple(replace(s, needs=tuple(n for n in s.needs if n in known)) for s in steps)
    return tuple(s if s.needs else replace(s, join="all") for s in kept)


# --------------------------------------------------------------------------------------------------
# the invariants
# --------------------------------------------------------------------------------------------------


def classify_row(record: StepRecord) -> str:
    """One need's recorded outcome, classified as the join table's own docstring classifies it.

    Written out here rather than imported from ``rayspec.engine.graph``: an oracle that asks the
    module under test what the right answer is agrees with it by construction. A change inside
    ``classify`` would move the expectation and the behaviour together, and this property — the
    one whose whole job is the truth table — could not see it. (Measured: with ``classify``
    imported, mutating ``FAILED and tolerated`` to ``FAILED or tolerated``, which classifies
    every untolerated failure as a success, left this file green.)
    """
    if record.status is StepStatus.SUCCEEDED:
        return "succeeded"
    if record.status is StepStatus.FAILED and record.tolerated:
        return "succeeded"
    if record.status is StepStatus.SKIPPED:
        return "skipped"
    if record.status in FAILED_STATUSES:
        return "failed"
    raise AssertionError(f"the join table only classifies terminal outcomes, got {record.status}")


def table_verdict(step: Gen, needs: list[StepRecord]) -> tuple[str, str | None]:
    """The join table's verdict: ``run`` / ``skip`` (+ reason) / ``drain`` (the ambiguous cell).

    ``drain`` is the row that runs unless the list happened to be draining when the step became
    ready — which no recorded outcome can pin down, so both outcomes are accepted for it.
    """
    classes = [classify_row(record) for record in needs]
    if step.join == "always":
        return "run", None
    if any(c == "failed" for c in classes):
        return "skip", "upstream_failed"
    if classes and all(c == "skipped" for c in classes):
        return "skip", "upstream_skipped"
    if step.join == "all" and any(c == "skipped" for c in classes):
        return "skip", "upstream_skipped"
    return "drain", None


def sibling_lists(
    case: Case, records: dict[str, StepRecord]
) -> list[tuple[str, tuple[Gen, ...], dict[str, StepRecord]]]:
    """Every recorded sibling list: ``(prefix, generated steps, {step id: record})``.

    The prefix is a record path minus its last segment (``""`` at root, ``s1[2]`` for the second
    iteration of a loop, ``s1[0]/s4`` for an include inside a fan-out item). Its last segment
    names the composite whose body the list is, so the generated steps come from that step's
    ``children`` — ids are unique across the whole case, which is what makes this lookup safe.
    """
    lookup = by_id(case.steps)
    grouped: dict[str, dict[str, StepRecord]] = {}
    for path, record in records.items():
        prefix, _, leaf = path.rpartition("/")
        grouped.setdefault(prefix, {})[leaf] = record
    out = []
    for prefix, entries in grouped.items():
        if prefix == "":
            out.append((prefix, case.steps, entries))
            continue
        owner = lookup.get(prefix.rsplit("/", 1)[-1].split("[")[0])
        if owner is not None:
            out.append((prefix, owner.children, entries))
    return out


def check_join_table(
    case: Case, records: dict[str, StepRecord], *, cancelled: bool
) -> Counter[str]:
    """Assert the table for every recorded sibling list; counts what was actually checked.

    A step whose needs are not all recorded is not checked: a body torn down mid-flight leaves
    part of its list undecided, and the table has nothing to say about a step nobody decided.

    The counters are what the tests then assert on — a generator that quietly stopped producing
    nested lists, skip rows or ``join: always`` rows would otherwise leave a green suite
    checking nothing. Every one of them is counted BEFORE the branches decide what to assert:
    counting inside a branch means the half of the table that ``continue``s is invisible to the
    guards, and "no skip row was drawn on this run" then reads exactly like a full table.
    """
    seen: Counter[str] = Counter()
    for prefix, steps, entries in sibling_lists(case, records):
        depth = prefix.count("/") + bool(prefix)
        for step in steps:
            record = entries.get(step.id)
            if record is None or any(need not in entries for need in step.needs):
                continue
            needs = [entries[need] for need in step.needs]
            classes = {classify_row(need) for need in needs}
            verdict, reason = table_verdict(step, needs)
            where = f"{prefix}/{step.id} (join {step.join}, needs {list(step.needs)})"
            seen["rows"] += 1
            seen[f"verdict:{verdict}"] += 1
            seen[f"depth:{depth}"] += 1
            seen["nested"] += bool(prefix)
            if step.join == "any" and classes == {"skipped", "succeeded"}:
                # the one row where the ``any`` column differs from the ``all`` column, and
                # therefore the only row that says what ``join: any`` is for
                seen["any_mixed"] += 1
            if step.join == "always" and step.when_false and cancelled:
                seen["always_when"] += 1
            if verdict == "skip":
                assert record.status is StepStatus.SKIPPED, f"{where}: expected skip, got {record}"
                allowed = {reason, "stopped"} if cancelled else {reason}
                assert record.skip_reason in allowed, where
                continue
            if step.when_false:
                assert record.status is StepStatus.SKIPPED, where
                assert record.skip_reason == "when_false" or record.skip_reason in DRAIN_REASONS
                continue
            if verdict == "run":
                seen["always"] += 1
                assert record.status in RAN, f"{where}: join always must run, got {record}"
                continue
            assert record.status in RAN or record.skip_reason in DRAIN_REASONS, where
    return seen


def failing(record: StepRecord) -> bool:
    """What a composite counts as a body failure (``scheduler``'s ``failed_body_step``).

    Spelled out over :data:`FAILED_STATUSES` for the same reason :func:`classify_row` is: the
    expectation must not move when the module under test does.
    """
    return record.status in FAILED_STATUSES and not record.tolerated


def body_records(path: str, kind: str, records: dict[str, StepRecord]) -> list[StepRecord]:
    """The composite's OWN sibling list: its direct body steps, over every iteration or item.

    Direct, never every descendant. A composite derives its status from the list it ran, and
    the composites in THAT list have already decided what their own bodies mean — an
    ``allow_failure: true`` include absorbs the failure below it, and a grandparent that
    re-counted the same record would contradict the run it just recorded.
    """
    out: list[StepRecord] = []
    for other, record in records.items():
        parent = other.rpartition("/")[0]
        if kind == "include":
            if parent == path:
                out.append(record)
        elif parent.startswith(f"{path}[") and "/" not in parent[len(path) :]:
            out.append(record)
    return out


def check_composites(case: Case, records: dict[str, StepRecord]) -> Counter[str]:
    """A composite is ``failed`` exactly when its body holds an untolerated failure.

    Only composites that ran to a terminal verdict of their own are checked: one that was
    skipped by the join table has no body, and one that was interrupted was cancelled with its
    body still in flight.
    """
    checked: Counter[str] = Counter()
    for path, record in records.items():
        step = owner_of(case, path)
        if step is None or step.kind not in {"loop", "each", "include"}:
            continue
        if record.status not in {StepStatus.SUCCEEDED, StepStatus.FAILED}:
            continue
        checked["nested"] += "/" in path
        bad = any(failing(rec) for rec in body_records(path, step.kind, records))
        if step.kind == "each":
            bad = bad and step.on_failure == "fail"
        expected = StepStatus.FAILED if bad else StepStatus.SUCCEEDED
        assert record.status is expected, (
            f"{path} ({step.kind}) is {record.status.value}; its body says {expected.value}"
        )
        checked[step.kind] += 1
    return checked


def check_teardown(case: Case, records: dict[str, StepRecord], *, raised: bool) -> None:
    """Draining never interrupts, a ``stop:`` bubbles iff a ``stop:`` ran, the root list settles.

    The last of the three is the wind-down's whole job: whatever tore the list down, every step
    of the ROOT list has a recorded verdict when ``run_graph`` returns. (A body torn down
    mid-flight may legitimately leave steps undecided — its parent decides what that means.)
    """
    if case.mode in {"drain", "continue"}:
        interrupted = [p for p, r in records.items() if r.status is StepStatus.INTERRUPTED]
        assert not interrupted, f"draining must let running steps finish, interrupted {interrupted}"
    stopped = [
        path
        for path, record in records.items()
        if (owner := owner_of(case, path)) is not None
        and owner.kind == "stop"
        and record.status is StepStatus.SUCCEEDED
    ]
    assert raised == bool(stopped), (
        f"RunStopped raised={raised} but the stop steps that ran are {stopped}"
    )
    for step in case.steps:
        assert step.id in records, f"the root list left {step.id} undecided"


# --------------------------------------------------------------------------------------------------
# the gate keeper: who opens a ``block:`` body, and when
# --------------------------------------------------------------------------------------------------


#: How often the keeper looks at the run, and how many turns of the event loop it then gives the
#: engine to prove it is still going. Neither is a margin an assertion rests on: a graph that is
#: getting somewhere moves :func:`pulse` between any two looks however they are spaced, and one
#: that is parked on a gate moves nothing however long anybody waits. They only decide how
#: promptly a stalled graph is let go, and how much the keeper costs while it watches.
GATE_LOOK_S = 0.005
GATE_LOOP_TURNS = 6


def pulse(harness: Harness, g: GraphHarness) -> tuple[int, int, int, int]:
    """Everything about a run that moves while the graph is getting somewhere.

    Leaves entering and leaving their bodies, the events the engine emitted, and whether a worker
    thread is busy — ``run.json`` is written through ``anyio.to_thread``, so a step in the middle
    of its own bookkeeping shows in none of the first three and plainly in the fourth. That last
    term is the one that earns its place: the instant a failing step's record lands is also the
    instant before its outcome reaches the scheduler, and without the thread reading that instant
    looks exactly like a graph with nothing left to do — the one moment the keeper must not open
    anything.

    A graph parked on a gate moves none of the four, ever, which is what makes "these stopped
    changing" a state the keeper reads rather than a duration it guesses.
    """
    return (
        len(g.leaf.started),
        len(g.leaf.finished),
        len(harness.sink.events),
        to_thread.current_default_thread_limiter().statistics().borrowed_tokens,
    )


def held_gates(g: GraphHarness, gates: frozenset[str], opened: set[str]) -> set[str]:
    """The gates a step is parked on right now and nobody has opened yet.

    A leaf is in flight between its entry in ``started`` and its entry in ``finished``, and the
    gate it is parked on is the last segment of its path (see :func:`gates_of`).
    """
    in_flight = set(g.leaf.started) - set(g.leaf.finished)
    return {path.rsplit("/", 1)[-1] for path in in_flight} & (gates - opened)


async def still_parked(harness: Harness, g: GraphHarness, seen: tuple[int, int, int, int]) -> bool:
    """Whether the run is still exactly where ``seen`` left it after the loop has been round.

    ``anyio.sleep(0)`` hands the event loop to every task that can run, so a step whose outcome
    was already in the scheduler's stream when the keeper looked gets delivered, acted on and
    recorded inside these turns. What survives them is a graph with nobody left to run.
    """
    for _ in range(GATE_LOOP_TURNS):
        await anyio.sleep(0)
        if pulse(harness, g) != seen:
            return False
    return True


async def keep_the_gates(harness: Harness, g: GraphHarness, gates: frozenset[str]) -> None:
    """Hold every ``block:`` step in its body until the graph cannot get past it, then let it go.

    This is the half of the gate that keeps a generated case from hanging, and the generator can
    put one anywhere: on a step the rest of the graph needs, on all four ``max_parallel`` slots at
    once, on a ``join: always`` cleanup that the WIND-DOWN launches after the teardown has already
    torn the task group down. So the rule is not "open after a while" but "open once the graph has
    stopped moving", which :func:`pulse` states exactly — and a graph parked on a gate has stopped
    moving for good, so the opening always comes.

    Opening late is the point and costs nothing: while a gate is held the graph either has other
    work, in which case it does it and says so, or it has not, in which case the next look says
    that. Opening EARLY is what would cost something — a step let go before a sibling failed or a
    ``stop:`` fired is a step the teardown no longer has anything to tear down — which is why a
    look that finds nothing moving is confirmed against the loop itself before it is believed.

    Only the gates parked right now are opened; the ones a later stage will reach stay shut, so a
    run's second and third teardown find as much in flight as its first.
    """
    opened: set[str] = set()
    last = pulse(harness, g)
    while True:
        await anyio.sleep(GATE_LOOK_S)
        now = pulse(harness, g)
        held = held_gates(g, gates, opened)
        if held and now == last and now[3] == 0 and await still_parked(harness, g, now):
            for name in held:
                g.leaf.releases[name].set()
            opened |= held
        last = pulse(harness, g)


async def under_the_keeper(
    harness: Harness, g: GraphHarness, gates: frozenset[str], body: Callable[[], Awaitable[T]]
) -> T:
    """Await ``body`` with :func:`keep_the_gates` alongside it, and open every gate on the way out.

    The unconditional opening at the end is what makes a gate safe to generate at all: a signal
    that bubbled, an assertion that failed and the 30s backstop all leave through here, and none
    of them may leave a step parked in a body — a property multiplies one hung case by every case
    the shrinker then tries.

    The one thing standing between the body and the driver is the task group, and a task group
    wraps whatever the body raised in an ``ExceptionGroup``. The driver reports the exception it
    caught, so wrapping would replace every falsified case's message with "unhandled errors in a
    TaskGroup (1 sub-exception)" — measured against a change that made a ``stop:`` outrank the
    pause, which this file's own report then said nothing about. A lone sub-exception is
    therefore handed on as itself. ``ExceptionGroup`` and not ``BaseExceptionGroup``: a group
    that carries a cancellation belongs to the scope that raised it and must leave as it came.
    """
    try:
        try:
            with anyio.fail_after(30):
                async with anyio.create_task_group() as tg:
                    if gates:
                        tg.start_soon(keep_the_gates, harness, g, gates)
                    result = await body()
                    tg.cancel_scope.cancel()  # the run is over; nothing left to keep
                    return result
            # a cancel scope may swallow what happened inside it; this one has nothing to
            # swallow — the body either returned above or the backstop raised
            raise AssertionError("the 30s backstop ended the run without saying so")
        except ExceptionGroup as group:
            if len(group.exceptions) != 1:
                raise
            raise group.exceptions[0] from None
    finally:
        for name in gates:
            g.leaf.releases[name].set()


# --------------------------------------------------------------------------------------------------
# the properties
# --------------------------------------------------------------------------------------------------


async def run_case(harness: Harness, case: Case) -> tuple[dict[str, StepRecord], bool]:
    """Write the generated project, run the root graph, return its records and whether it stopped.

    The graph runs :func:`under_the_keeper`, which is what makes a ``block:`` body safe to
    generate: every gate is opened once the graph has nothing else to do, and again —
    unconditionally, on every way out — when the run is over.
    """
    for name, text in case.includes().items():
        harness.workflow(name, text)
    harness.workflow("t", case.workflow_yaml())
    options = RunOptions(fail_fast=case.mode == "fail_fast")
    g = make_graph_harness(harness, harness.load("t"), options=options)

    async def run() -> bool:
        try:
            await run_graph(g.graph, g.scope, g.ctx)
        except RunStopped:
            return True
        return False

    raised = await under_the_keeper(harness, g, gates_of(case.steps), run)
    started = list(g.leaf.started)
    records = harness.record(g.run.run_id).steps
    for step in walk(case.steps):
        if step.when_false:
            assert not any(p.rsplit("/", 1)[-1] == step.id for p in started), (
                f"{step.id} has when: false and must never be dispatched"
            )
    return records, raised


@pytest.mark.parametrize("mode", MODES)
async def test_the_join_table_holds_inside_composites(harness: Harness, mode: str) -> None:
    """The plan's truth table, per sibling list, at every nesting depth."""
    seen: Counter[str] = Counter()

    async def prop(case: Case) -> None:
        records, raised = await run_case(harness, case)
        seen.update(check_join_table(case, records, cancelled=raised))

    await aforall(
        f"join-table-{mode}",
        case_for(mode),
        prop,
        cases=CASES,
        shrink=shrink_case,
        show=str,
        examples=(mixed_join_row_case(mode),),
    )
    assert seen["always"] > 0, f"the generator produced no join: always row to check: {seen}"
    assert seen["nested"] > 0, f"every checked row was at the root: {seen}"
    assert seen["depth:2"] > 0, f"no sibling list two levels down was checked: {seen}"
    assert seen["verdict:skip"] > 0, f"no row of the table's skip half was drawn: {seen}"
    assert seen["any_mixed"] > 0, (
        f"no join: any step ever saw a mix of skipped and succeeded needs — the row that is "
        f"the whole reason join: any exists went unchecked: {seen}. "
        f"mixed_join_row_case() is passed as an example so this cannot depend on the seed; "
        f"if it fails, that case stopped reaching the row"
    )
    if mode == "stop":
        assert seen["always_when"] > 0, (
            f"no join: always step carrying a when: was decided by the wind-down: {seen}"
        )


@pytest.mark.parametrize("mode", MODES)
async def test_a_composite_reports_what_its_body_did(harness: Harness, mode: str) -> None:
    """``loop:``/``each:``/``include:`` fail exactly when their body holds a real failure."""
    seen: Counter[str] = Counter()

    async def prop(case: Case) -> None:
        records, _ = await run_case(harness, case)
        seen.update(check_composites(case, records))

    await aforall(
        f"composite-status-{mode}", case_for(mode), prop, cases=CASES, shrink=shrink_case, show=str
    )
    for kind in ("loop", "each", "include"):
        assert seen[kind] > 0, f"the generator produced no {kind}: step that ran: {seen}"
    assert seen["nested"] > 0, f"every checked composite was at the root: {seen}"


@pytest.mark.parametrize("mode", MODES)
async def test_teardown_discipline(harness: Harness, mode: str) -> None:
    """Drain finishes what is running; a ``stop:`` bubbles iff a ``stop:`` step ran; and the
    root list is always fully decided — the wind-down's whole job."""

    seen: Counter[str] = Counter()

    async def prop(case: Case) -> None:
        records, raised = await run_case(harness, case)
        check_teardown(case, records, raised=raised)
        seen["stopped"] += raised
        seen["interrupted"] += sum(
            1 for r in records.values() if r.status is StepStatus.INTERRUPTED
        )
        for path in records:
            seen[f"depth:{path.count('/')}"] += 1

    await aforall(
        f"teardown-{mode}",
        case_for(mode),
        prop,
        cases=CASES,
        shrink=shrink_case,
        show=str,
        examples=torn_down_cases(mode),
    )
    assert seen["depth:2"] > 0, f"nothing two levels down was ever recorded: {seen}"
    if mode == "stop":
        assert seen["stopped"] > 0, "no generated stop: step ever fired"
    if mode in {"stop", "fail_fast"}:
        assert seen["interrupted"] > 0, (
            f"nothing was ever in flight when the teardown began: no block: step was still parked "
            f"in its body when a stop: fired or a failure tore its list down ({seen}). "
            f"torn_down_cases() is passed as an example so this cannot depend on the seed; "
            f"if it fails, those cases stopped reaching the shape"
        )


# --------------------------------------------------------------------------------------------------
# the wind-down: a cleanup gate that pauses
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PauseCase:
    """A teardown with a human gate in it: a DAG, a ``stop:``, a ``join: always`` gate, a tail.

    The gate is the cleanup step an operator is asked to answer ("shall I roll back?"), so it
    sits behind ``join: always`` — reachable *because* the run is being torn down. Everything in
    ``tail`` needs the gate and therefore must stay undecided until somebody answers it.
    """

    before: tuple[Gen, ...]
    tail: tuple[Gen, ...]
    nested: bool

    def workflow_yaml(self) -> str:
        """The root workflow: the surroundings, the ``stop:``, the gate, then the tail."""
        halt = Gen(id="halt", kind="stop")
        gate_line = ["  - id: gate", "    needs: [halt]", "    join: always"]
        if self.nested:
            gate_line += ["    include: inc_gate"]
        else:
            gate_line += ['    approve: "roll back?"']
        lines = [*body_yaml(self.before, 2), *halt.yaml(2), *gate_line]
        lines += body_yaml(
            tuple(replace(step, needs=("gate",), join="always") for step in self.tail), 2
        )
        return "rayspec: 1\nname: t\nsteps:\n" + "\n".join(lines)

    def includes(self) -> dict[str, str]:
        """``{file stem: yaml}`` for every included body, the gate's own body included."""
        out = {
            include_name(step.id): (
                f"rayspec: 1\nname: {include_name(step.id)}\nsteps:\n"
                + "\n".join(body_yaml(step.children, 2))
            )
            for step in [*walk(self.before), *walk(self.tail)]
            if step.kind == "include"
        }
        if self.nested:
            out["inc_gate"] = 'rayspec: 1\nname: inc_gate\nsteps:\n  - {id: g, approve: "ok?"}\n'
        return out

    def __str__(self) -> str:
        parts = [f"nested={self.nested}", self.workflow_yaml()]
        parts += [f"# {name}.yaml\n{text}" for name, text in self.includes().items()]
        return "\n".join(parts)


def pause_case(rng: random.Random) -> PauseCase:
    """A teardown-with-a-gate case: random surroundings, a random tail behind the gate."""
    counter = [100]
    before = gen_steps(rng, counter, depth=1, size=rng.randint(1, 3))
    tail = gen_steps(rng, counter, depth=1, size=rng.randint(1, 3))
    return PauseCase(
        before=before,
        tail=tuple(replace(s, when_false=False) for s in tail),
        nested=rng.random() < 0.4,
    )


def shrink_pause(case: PauseCase):
    """Simpler pause cases: a shorter tail, fewer surroundings, then the gate at the root."""
    for smaller in shrink_seq(case.tail):
        if smaller:
            yield replace(case, tail=repair(tuple(smaller)))
    for smaller in shrink_seq(case.before):
        if smaller:
            yield replace(case, before=repair(tuple(smaller)))
    if case.nested:
        yield replace(case, nested=False)


async def test_a_gate_that_pauses_ends_the_wind_down(harness: Harness) -> None:
    """A cleanup gate reached by the teardown pauses the run and stops the wind-down there.

    Three things have to hold together, and each of them was a way to make the gate
    unanswerable: the PAUSE bubbles rather than the ``stop:`` that started the teardown (a run
    recorded ``failed`` cannot be approved); the gate's own record is ``paused``, not
    ``interrupted``; and nothing behind the gate is decided, so the resumed run still has a
    cleanup to run. Generated over random surroundings, with the gate both at the root and
    inside an ``include:`` body, because the shape of what is in flight is exactly what broke it.
    """
    from rayspec.engine.errors import RunPaused

    async def prop(case: PauseCase) -> None:
        for name, text in case.includes().items():
            harness.workflow(name, text)
        harness.workflow("t", case.workflow_yaml())
        g = make_graph_harness(harness, harness.load("t"), options=RunOptions(interactive=False))

        async def run() -> Raised[RunPaused]:
            with raises(RunPaused) as caught:
                await run_graph(g.graph, g.scope, g.ctx)
            return caught

        # the surroundings hold ``block:`` steps too, and the wind-down launches the ``join:
        # always`` ones among them AFTER the stop tore the task group down — a gate reached there
        # is one nothing else will ever open
        gates = gates_of((*case.before, *case.tail))
        info = await under_the_keeper(harness, g, gates, run)
        records = harness.record(g.run.run_id).steps
        assert info.value.token.startswith("gate"), info.value.token
        assert records["gate"].status is StepStatus.PAUSED, records["gate"]
        assert records["halt"].status is StepStatus.SUCCEEDED
        for step in case.tail:
            assert step.id not in records, (
                f"{step.id} needs the gate and must stay undecided until it is answered"
            )

    await aforall("wind-down-pause", pause_case, prop, cases=20, shrink=shrink_pause, show=str)
