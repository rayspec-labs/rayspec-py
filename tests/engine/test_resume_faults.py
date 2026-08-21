"""Crash a run at every persistence point; resume must converge on the same final state.

``run.json`` is rewritten after every step, the output file is written before the record, and the
resume cache replays a step only when its record is reusable *and* its output file exists. Those
are promises about torn state, so they are tested by tearing it: ``_faulty_store.FaultyStore``
kills the store at the n-th ``save`` / ``write_output`` / ``append_event`` / ``append_stream``,
and nothing is persisted afterwards — the closest in-process approximation of ``kill -9``.

For every crash point the assertion is the same: resuming the killed run ends in the same status,
with the same per-step statuses and the same rendered outputs as an uninterrupted run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.runner import Runner, RunResult, Workspace
from rayspec.providers.stub import StubScript
from rayspec.schema import RunStatus
from rayspec.store.file import FileRunStore

from ._faulty_store import FaultPoint, FaultyStore, StoreCrash, enumerate_points

pytestmark = pytest.mark.anyio

WORKFLOW = """
rayspec: 1
name: faulty
description: A run with every persistence shape — prompt, structured output, loop, skip, shell.

inputs:
  issue: { type: integer, default: 7 }

isolation: none

agents:
  triage: { provider: claude, model: small, access: read-only }

steps:
  - id: assess
    agent: triage
    prompt: "Assess {{ inputs.issue }}"
    output_schema:
      type: object
      properties:
        verdict: { type: string }
      required: [verdict]

  - id: bail
    needs: [assess]
    when: steps.assess.output.verdict == 'skip'
    shell: "echo skipped"

  - id: build
    needs: [assess]
    when: steps.assess.output.verdict == 'fix'
    loop:
      max_iterations: 2
      until: steps.review.output | has_signal('CLEAN')
      steps:
        - id: implement
          agent: triage
          prompt: "Implement round {{ iteration.n }}"
        - id: review
          needs: [implement]
          agent: triage
          prompt: "Review round {{ iteration.n }}"

  - id: wrap
    needs: [build]
    shell: "echo done"

outputs:
  verdict: "{{ steps.assess.output.verdict }}"
  rounds: "{{ steps.build.iterations }}"
"""

#: Keyed by the RECORD path, never by ``sequence:``. A sequence counter lives in the stub
#: provider instance, so a resumed run — a new process — would replay it from the start and the
#: loop could not converge; a path-keyed script is idempotent, which is what a crash test needs.
STUBS: dict[str, Any] = {
    "steps": {
        "assess": {"output": {"verdict": "fix"}},
        "build[*]/implement": {"text": "a cut"},
        "build[1]/review": {"text": "needs work"},
        "build[2]/review": {"text": "CLEAN"},
    },
    "defaults": {"latency_ms": 0},
}

RUN_ID = "20260821-000000-flt1"


def make_runner(harness: Any, store: Any, *, resume: str | None = None) -> Runner:
    """A runner for the faulty workflow against ``store`` (dry run + the stub script)."""
    return Runner(
        harness.load("faulty"),
        inputs={"issue": 7},
        store=store,
        project_root=harness.root,
        project_slug="local/test",
        workspace=Workspace.in_place(harness.root),
        options=RunOptions(dry_run=True, stub_script=StubScript.from_dict(STUBS), force=True),
        run_id=None if resume else RUN_ID,
        resume_run_id=resume,
        handle_signals=False,
    )


def final_state(result: RunResult) -> tuple[str, dict[str, str], dict[str, Any]]:
    """What "the same final state" means: status, per-step statuses, rendered outputs."""
    return (
        result.status.value,
        {path: record.status.value for path, record in sorted(result.steps.items())},
        dict(result.outputs or {}),
    )


@pytest.fixture
def project(harness: Any) -> Any:
    harness.workflow("faulty", WORKFLOW)
    return harness


async def clean_run(project: Any, root: Path) -> tuple[FaultyStore, RunResult]:
    """One uninterrupted run through a counting (never crashing) store."""
    store = FaultyStore(FileRunStore(root))
    result = await make_runner(project, store).run()
    return store, result


async def _baseline(project: Any, tmp_path: Path) -> tuple[dict[str, int], Any]:
    store, result = await clean_run(project, tmp_path / "baseline")
    return dict(store.counts), final_state(result)


def _points(counts: dict[str, int]) -> list[FaultPoint]:
    return enumerate_points(counts, per_method=4)


async def test_the_uninterrupted_run_is_the_reference(project: Any, tmp_path: Path) -> None:
    counts, state = await _baseline(project, tmp_path)
    status, statuses, outputs = state
    assert status == RunStatus.SUCCEEDED.value
    assert statuses["bail"] == "skipped"
    assert statuses["build[2]/review"] == "succeeded"
    assert outputs == {"verdict": "fix", "rounds": 2}
    assert all(counts[method] > 0 for method in ("save", "write_output", "append_event"))
    assert len(_points(counts)) >= 10, counts


async def test_crash_points_cover_every_persistence_method(project: Any, tmp_path: Path) -> None:
    counts, _ = await _baseline(project, tmp_path)
    points = _points(counts)
    methods = {point.method for point in points}
    assert methods == {"save", "write_output", "append_event", "append_stream"}
    assert {point.when for point in points} == {"before", "after", "torn"}
    torn = {point.method for point in points if point.when == "torn"}
    assert torn == {"append_event", "append_stream"}, torn


@pytest.mark.parametrize("index", range(18))
async def test_resume_converges_from_every_crash_point(
    project: Any, tmp_path: Path, index: int
) -> None:
    counts, baseline = await _baseline(project, tmp_path)
    points = _points(counts)
    if index >= len(points):
        pytest.skip(f"only {len(points)} crash points for this workflow")
    point = points[index]

    root = tmp_path / f"crash-{index}"
    store = FaultyStore(FileRunStore(root), fault=point)
    with pytest.raises(BaseException) as excinfo:
        await make_runner(project, store).run()
    assert _has_crash(excinfo.value), f"{point}: died of {excinfo.value!r}"
    assert store.crashed

    _kill_the_process(root)
    resumed = await make_runner(project, FileRunStore(root), resume=RUN_ID).run()
    assert final_state(resumed) == baseline, f"{point}: resume did not converge"


@pytest.mark.parametrize("method", ["append_event", "append_stream"])
async def test_a_torn_jsonl_line_is_tolerated(project: Any, tmp_path: Path, method: str) -> None:
    """The one durability promise specific to JSONL: readers tolerate a half-written line.

    ``FileRunStore`` documents that a crash mid-write leaves a torn trailing line and that
    ``read_events``/``read_stream`` end the iteration there rather than raising. A fault point
    that dies *before or after* a whole call can never produce one, so this crashes in the middle
    of the line itself.
    """
    counts, baseline = await _baseline(project, tmp_path)
    point = FaultPoint(method, max(2, counts[method] // 2), "torn")
    root = tmp_path / f"torn-{method}"
    store = FaultyStore(FileRunStore(root), fault=point)
    with pytest.raises(BaseException) as excinfo:
        await make_runner(project, store).run()
    assert _has_crash(excinfo.value)

    real = FileRunStore(root)
    path = store.torn_path
    assert path is not None and path.name.endswith(".jsonl"), path
    text = path.read_text(encoding="utf-8")
    assert not text.endswith("\n"), f"{path} has no torn trailing line"
    assert text.rsplit("\n", 1)[-1], "the torn prefix was not written"

    assert list(real.read_events(RUN_ID)), "read_events raised or stopped at the first line"
    if method == "append_stream":
        step = path.parent.relative_to(real.run_dir(RUN_ID) / "steps").as_posix()
        assert list(real.read_stream(RUN_ID, step)), "read_stream lost every record of the step"

    _kill_the_process(root)
    resumed = await make_runner(project, FileRunStore(root), resume=RUN_ID).run()
    assert final_state(resumed) == baseline, f"{point}: resume did not converge"
    assert list(FileRunStore(root).read_events(RUN_ID))


async def test_a_crash_leaves_the_record_unfinished(project: Any, tmp_path: Path) -> None:
    """The point of the exercise: after a crash ``run.json`` is not a finished record."""
    counts, _ = await _baseline(project, tmp_path)
    point = FaultPoint("save", max(2, counts["save"] // 2), "after")
    root = tmp_path / "torn"
    store = FaultyStore(FileRunStore(root), fault=point)
    with pytest.raises(BaseException) as excinfo:
        await make_runner(project, store).run()
    assert _has_crash(excinfo.value)
    record = FileRunStore(root).load(RUN_ID)
    assert record.status is RunStatus.RUNNING
    assert record.ended_at is None


async def test_resume_reuses_the_steps_that_survived(project: Any, tmp_path: Path) -> None:
    """A crash late in the run must not re-run everything: survivors are replayed."""
    counts, baseline = await _baseline(project, tmp_path)
    point = FaultPoint("save", counts["save"] - 1, "after")
    root = tmp_path / "late"
    store = FaultyStore(FileRunStore(root), fault=point)
    with pytest.raises(BaseException) as excinfo:
        await make_runner(project, store).run()
    assert _has_crash(excinfo.value)
    _kill_the_process(root)
    resumed = await make_runner(project, FileRunStore(root), resume=RUN_ID).run()
    assert final_state(resumed) == baseline
    assert resumed.reused, "a late crash should replay the finished steps"


def _kill_the_process(root: Path) -> None:
    """A real crash leaves no live process: clear the pid the resume guard checks."""
    store = FileRunStore(root)
    record = store.load(RUN_ID)
    record.pid = None
    store.save(record)


def _has_crash(error: BaseException) -> bool:
    """Whether ``error`` is (or wraps) the injected :class:`StoreCrash`."""
    if isinstance(error, StoreCrash):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_has_crash(inner) for inner in error.exceptions)
    cause = error.__cause__ or error.__context__
    return bool(cause and _has_crash(cause))
