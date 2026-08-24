"""Shared fixtures for engine tests: a temp project + store + collecting sink + run helpers."""

from __future__ import annotations

import itertools
import textwrap
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest

from rayspec.config import Config
from rayspec.engine.context import ExecScope, RunContext, RunOptions, StepOutcome
from rayspec.engine.graph import StepGraph
from rayspec.engine.paths import StepPath
from rayspec.engine.runner import Runner, RunResult, Workspace
from rayspec.engine.runtime import Runtime
from rayspec.events.model import EventType, RunEvent
from rayspec.events.sinks import CollectingSink
from rayspec.loader import ResolvedWorkflow, load_workflow
from rayspec.providers.base import Provider
from rayspec.schema import RunStatus, StepModel, StepStatus
from rayspec.store.file import FileRunStore
from rayspec.store.model import RunRecord, StepRecord
from rayspec.templating import Scope, StepView, TemplateEngine


@dataclass
class Harness:
    """A temp project (``root/.rayspec``), a file store and a collecting sink."""

    tmp: Path
    root: Path
    home: Path
    store: FileRunStore
    sink: CollectingSink = field(default_factory=CollectingSink)
    engine: TemplateEngine = field(default_factory=TemplateEngine)

    # -- project files --------------------------------------------------------------------

    def write(self, rel: str, text: str) -> Path:
        path = self.root / ".rayspec" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return path

    def workflow(self, name: str, text: str) -> Path:
        return self.write(f"workflows/{name}.yaml", text)

    def load(self, name: str) -> ResolvedWorkflow:
        return load_workflow(name, project_root=self.root, home=self.home, config=Config())

    # -- running ----------------------------------------------------------------------------

    def runner(
        self,
        resolved: ResolvedWorkflow | str,
        inputs: Mapping[str, Any] | None = None,
        *,
        options: RunOptions | None = None,
        providers: Mapping[str, Provider] | None = None,
        prompt: Any = None,
        resume: str | None = None,
        run_id: str | None = None,
        workspace: Workspace | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Runner:
        if isinstance(resolved, str):
            resolved = self.load(resolved)
        return Runner(
            resolved,
            inputs=inputs or {},
            store=self.store,
            sinks=self.sink,
            project_root=self.root,
            project_slug="local/test",
            options=options,
            providers=providers,
            approval_prompt=prompt,
            engine=self.engine,
            resume_run_id=resume,
            run_id=run_id,
            workspace=workspace,
            env=env,
            handle_signals=True,
        )

    async def run(self, resolved: ResolvedWorkflow | str, inputs=None, **kw: Any) -> RunResult:
        return await self.runner(resolved, inputs, **kw).run()

    # -- inspection -------------------------------------------------------------------------

    def record(self, run_id: str) -> RunRecord:
        return self.store.load(run_id)

    def events(self, type_: EventType | None = None) -> list[RunEvent]:
        if type_ is None:
            return list(self.sink.events)
        return self.sink.events_of(type_)

    def finished(self, path: str) -> RunEvent:
        for event in self.sink.events_of(EventType.STEP_FINISHED):
            if event.step_path == path:
                return event
        raise AssertionError(f"no step.finished for {path}")

    def statuses(self, run_id: str) -> dict[str, str]:
        return {p: r.status.value for p, r in self.record(run_id).steps.items()}


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    return Harness(tmp=tmp_path, root=root, home=home, store=FileRunStore(tmp_path / "store"))


# --------------------------------------------------------------------------------------------------
# scheduler-level helpers: a RunContext with a fake leaf executor
# --------------------------------------------------------------------------------------------------


@dataclass
class FakeLeaf:
    """Fake shell executor keyed by the step's body text:

    ``ok`` / ``ok:<text>`` → succeeded (output text) · ``fail`` → failed · ``sleep:<s>`` →
    sleep then succeed · ``block`` → wait until :attr:`release` is set · ``block:<name>`` →
    wait until ``releases[name]`` is set · ``boom`` → raises (must become a failed step) ·
    ``transient`` → failed transient · ``hang`` → sleep forever.
    Records concurrency (``peak``) and the order of starts/finishes.

    **Prefer ``block:<name>`` over ``sleep:<s>`` to hold a step in flight.** A test that wants a
    sibling to still be running when it looks is asserting about a *state*, and a sleep only
    guesses at one: the engine's own bookkeeping per step is ``save_record`` + ``persist``, each a
    ``to_thread(store.save)`` under one lock, each writing a temp file, fsyncing it, then renaming
    it — 50-75 ms per step measured here, the same order as a ``sleep:0.05`` and *larger* than a
    ``sleep:0.01``. Whichever of the two wins is the machine's business, so the sleep
    decides the test. ``block:<name>`` makes it a fact instead: hold the sibling, wait for the
    state, assert, then ``leaf.releases[name].set()``. Keep ``sleep:`` only where a duration is
    genuinely what is under test (a timeout firing) and give it two orders of magnitude of room.

    ``releases`` and ``arrivals`` both mint their event on first mention from either side, so a
    test may set a gate before the step reaches it and await an arrival before the step exists.
    """

    started: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    running: int = 0
    peak: int = 0
    release: anyio.Event = field(default_factory=anyio.Event)
    #: ``name`` → the event a ``block:<name>`` body waits on; set it to let that step finish.
    releases: dict[str, anyio.Event] = field(default_factory=lambda: defaultdict(anyio.Event))
    #: step path → an event set the moment that step entered the body (see :meth:`wait_started`).
    arrivals: dict[str, anyio.Event] = field(default_factory=lambda: defaultdict(anyio.Event))
    calls: dict[str, int] = field(default_factory=dict)

    async def wait_started(self, *paths: str, timeout: float = 30.0) -> None:
        """Return once every named step path has entered the body.

        The counterpart to ``block:<name>``: a test holds its siblings open, waits here for the
        ones it needs in flight, and only then asserts. The wait is the assertion — a step the
        engine refuses to launch never arrives, so "these two really did overlap" is a fact the
        test established rather than a moment it sampled.

        ``timeout`` is not a race the test can lose: a step that is going to start starts within
        one round of the engine's own bookkeeping (~50-75 ms), so 30 s is some two orders of
        magnitude of headroom. It is here only so that a step which will NEVER start fails the
        test by name instead of deadlocking the suite — pytest has no timeout of its own, and a
        hung run reports nothing at all about which test hung or why.
        """
        for path in paths:
            try:
                with anyio.fail_after(timeout):
                    await self.arrivals[path].wait()
            except TimeoutError:
                raise AssertionError(
                    f"step {path!r} never started within {timeout:g}s — it was expected to be in "
                    f"flight alongside the others. Started so far: {self.started}"
                ) from None

    async def __call__(
        self, step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, attempt: int
    ) -> StepOutcome:
        body = getattr(step, "shell", "ok")
        if "{{" in body:  # the real executor renders; the fake renders as text
            body = ctx.engine.render_str(body, ctx.template_context(scope))
        self.started.append(record.path)
        self.calls[record.path] = self.calls.get(record.path, 0) + 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        self.arrivals[record.path].set()
        try:
            if body.startswith("sleep:"):
                await anyio.sleep(float(body.split(":", 1)[1]))
            elif body == "block":
                await self.release.wait()
            elif body.startswith("block:"):
                await self.releases[body.split(":", 1)[1]].wait()
            elif body == "hang":
                await anyio.sleep(3600)
            elif body == "boom":
                raise RuntimeError("kaboom")
            if body == "fail" or body.startswith("fail:"):
                record.status = StepStatus.FAILED
                record.ok = False
                from rayspec.store.model import ErrorInfo

                record.error = ErrorInfo(type="exit", message="exit code 1", transient=False)
                return StepOutcome(record=record, output="", output_kind="text")
            if body == "transient":
                from rayspec.store.model import ErrorInfo

                record.status = StepStatus.FAILED
                record.ok = False
                record.error = ErrorInfo(type="api", message="flaky", transient=True)
                return StepOutcome(record=record)
            record.status = StepStatus.SUCCEEDED
            record.ok = True
            text = body.split(":", 1)[1] if body.startswith("ok:") else record.id
            return StepOutcome(record=record, output=text, output_kind="text")
        finally:
            self.running -= 1
            self.finished.append(record.path)


#: How often :func:`wait_for_slot_queue` looks again. Not a margin: the loop ends on the queue
#: length, never on elapsed time, so this only trades CPU against how soon the fact is noticed.
_POLL = 0.005


async def wait_for_slot_queue(ctx: RunContext, waiting: int = 1, *, timeout: float = 30.0) -> None:
    """Return once at least ``waiting`` leaves are queued for a ``max_parallel`` slot.

    The fact an *upper-bound* assertion needs, and it is not the same fact as "these steps have
    started". A test that gates six steps, waits for the first two to arrive and then reads
    ``leaf.peak`` passes just as happily with the cap removed: at the instant the second step
    enters, the other four are still inside their own bookkeeping and have not entered anything
    — the reading is taken before the steps a broken cap would have let in could show up.
    Measured on this harness: with the limiter widened to 64, all six do enter, but ~0.3 s later.

    Waiting for the queue instead closes that hole. Once four leaves are parked on the limiter
    the engine has launched everything it is going to launch and the ones holding slots are all
    it will let run at once, so ``peak`` read here is the run's real ceiling. With no cap nobody
    ever queues, and this fails by name rather than passing quietly.

    ``timeout`` is a loudness backstop, not a race: the queue forms as fast as the engine can
    launch (~0.3 s for six steps here), so 30 s is two orders of magnitude of headroom, and it
    exists so a queue that will never form fails the test instead of hanging the suite.
    """
    limiter = ctx.runtime.leaf_limiter
    try:
        with anyio.fail_after(timeout):
            while limiter.statistics().tasks_waiting < waiting:
                await anyio.sleep(_POLL)
    except TimeoutError:
        stats = limiter.statistics()
        raise AssertionError(
            f"expected at least {waiting} leaf/leaves queued for a max_parallel slot within "
            f"{timeout:g}s, saw {stats.tasks_waiting} queued and {stats.borrowed_tokens} of "
            f"{stats.total_tokens} slot(s) taken"
        ) from None


@dataclass
class GraphHarness:
    ctx: RunContext
    scope: ExecScope
    graph: StepGraph
    leaf: FakeLeaf
    run: RunRecord


_RUN_COUNTER = itertools.count(1)


def make_graph_harness(
    harness: Harness,
    resolved: ResolvedWorkflow,
    *,
    options: RunOptions | None = None,
    fake_leaf: bool = True,
    providers: Mapping[str, Provider] | None = None,
    prompt: Any = None,
    run_id: str | None = None,
) -> GraphHarness:
    """A RunContext + root ExecScope + StepGraph for scheduler/executor tests."""
    run = RunRecord(
        run_id=run_id or f"20260820-000000-t{next(_RUN_COUNTER):03d}",
        workflow_name=resolved.workflow.name,
        workflow_path=resolved.label,
        workflow_hash=resolved.hash,
        project_slug="local/test",
        project_root=str(harness.root),
        status=RunStatus.RUNNING,
    )
    harness.store.create(run)
    runtime = Runtime(resolved.workflow.defaults.max_parallel)
    ctx = RunContext(
        resolved=resolved,
        run=run,
        store=harness.store,
        sinks=harness.sink,
        engine=harness.engine,
        runtime=runtime,
        options=options or RunOptions(),
        workdir=harness.root,
        project={"root": str(harness.root), "name": "proj", "slug": "local/test"},
        env={},
        approval_prompt=prompt,
        providers=providers,
    )
    leaf = FakeLeaf()
    if fake_leaf:
        ctx.executors["shell"] = leaf
        ctx.executors["python"] = leaf
    views: dict[str, StepView] = {}
    scope = ExecScope(
        prefix=StepPath.root(),
        def_prefix="",
        tscope=Scope(None, views),
        views=views,
        inputs={},
        defaults=resolved.workflow.defaults,
    )
    return GraphHarness(ctx, scope, StepGraph.from_steps(resolved.workflow.steps), leaf, run)


__all__ = [
    "FakeLeaf",
    "GraphHarness",
    "Harness",
    "make_graph_harness",
    "wait_for_slot_queue",
]
