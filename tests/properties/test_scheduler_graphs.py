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
from dataclasses import dataclass, replace

import anyio
import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.errors import RunStopped
from rayspec.engine.scheduler import run_graph
from rayspec.schema import StepStatus
from rayspec.store.model import StepRecord

from .conftest import Harness, make_graph_harness
from .generate import DEFAULT_CASES, aforall, raises, shrink_seq

pytestmark = pytest.mark.anyio

#: Statuses a step can only have because it was actually dispatched.
RAN = frozenset({StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.INTERRUPTED})
#: Terminal statuses the join table counts as a failure — restated here, see :func:`classify_row`.
FAILED_STATUSES = frozenset({StepStatus.FAILED, StepStatus.INTERRUPTED, StepStatus.REJECTED})
#: ``skip_reason`` values that mean "the graph ended", not "this step's needs decided it".
DRAIN_REASONS = frozenset({"run_failed", "stopped"})
#: Leaf bodies the fake executor understands; the sleeps put siblings in flight concurrently,
#: which is the only state in which a teardown has anything to tear down.
BODIES = ("ok", "ok", "fail", "sleep:0.01", "sleep:0.05")
#: The four failure policies a generated case is run under.
MODES = ("drain", "fail_fast", "continue", "stop")
#: Generated cases per property and mode. ``RAYSPEC_PROP_CASES`` raises it for a longer hunt.
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
        steps.append(
            Gen(
                id=sid,
                kind=kind,
                needs=needs,
                join=rng.choice(["all", "any", "any", "always"]) if needs else "all",
                body=rng.choice(BODIES),
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
# the properties
# --------------------------------------------------------------------------------------------------


async def run_case(harness: Harness, case: Case) -> tuple[dict[str, StepRecord], bool]:
    """Write the generated project, run the root graph, return its records and whether it stopped."""
    for name, text in case.includes().items():
        harness.workflow(name, text)
    harness.workflow("t", case.workflow_yaml())
    options = RunOptions(fail_fast=case.mode == "fail_fast")
    g = make_graph_harness(harness, harness.load("t"), options=options)
    raised = False
    with anyio.fail_after(30):
        try:
            await run_graph(g.graph, g.scope, g.ctx)
        except RunStopped:
            raised = True
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
        f"join-table-{mode}", case_for(mode), prop, cases=CASES, shrink=shrink_case, show=str
    )
    assert seen["always"] > 0, f"the generator produced no join: always row to check: {seen}"
    assert seen["nested"] > 0, f"every checked row was at the root: {seen}"
    assert seen["depth:2"] > 0, f"no sibling list two levels down was checked: {seen}"
    assert seen["verdict:skip"] > 0, f"no row of the table's skip half was drawn: {seen}"
    assert seen["any_mixed"] > 0, (
        f"no join: any step ever saw a mix of skipped and succeeded needs — the row that is "
        f"the whole reason join: any exists went unchecked: {seen}"
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
        f"teardown-{mode}", case_for(mode), prop, cases=CASES, shrink=shrink_case, show=str
    )
    assert seen["depth:2"] > 0, f"nothing two levels down was ever recorded: {seen}"
    if mode == "stop":
        assert seen["stopped"] > 0, "no generated stop: step ever fired"
    if mode in {"stop", "fail_fast"}:
        assert seen["interrupted"] > 0, "nothing was ever in flight when the teardown began"


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
        with anyio.fail_after(30):
            with raises(RunPaused) as info:
                await run_graph(g.graph, g.scope, g.ctx)
        records = harness.record(g.run.run_id).steps
        assert info.value.token.startswith("gate"), info.value.token
        assert records["gate"].status is StepStatus.PAUSED, records["gate"]
        assert records["halt"].status is StepStatus.SUCCEEDED
        for step in case.tail:
            assert step.id not in records, (
                f"{step.id} needs the gate and must stay undecided until it is answered"
            )

    await aforall("wind-down-pause", pause_case, prop, cases=20, shrink=shrink_pause, show=str)
