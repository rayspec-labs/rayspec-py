"""End-to-end style: an engine-like run loop persisting through FileRunStore and observing via sinks."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from rayspec.events import (
    CollectingSink,
    EventType,
    JsonStdoutSink,
    MultiSink,
    QuietConsoleSink,
    RunEvent,
    StreamRecord,
)
from rayspec.schema import RunStatus, StepStatus
from rayspec.store import FileRunStore, RunRecord, StepRecord, new_run_id

pytestmark = pytest.mark.anyio


async def test_run_persist_observe_and_resume(tmp_path: Path):
    store = FileRunStore(tmp_path / "proj")
    out = io.StringIO()
    console = Console(record=True, width=100, force_terminal=False, color_system=None)
    collected = CollectingSink()
    sink = MultiSink(JsonStdoutSink(out), QuietConsoleSink(console), collected)

    run = RunRecord(
        run_id=new_run_id(),
        workflow_name="demo",
        workflow_path="/p/.rayspec/workflows/demo.yaml",
        workflow_hash="sha256:0",
        project_slug="local/p",
        project_root="/p",
    )
    store.create(run)

    async def publish(event: RunEvent) -> None:
        store.append_event(run.run_id, event)  # persist first ...
        await sink.emit(event)  # ... then observe

    await publish(RunEvent(type=EventType.RUN_STARTED, run_id=run.run_id))

    # a prompt step streaming deltas, then succeeding with text output
    path = "build[1]/implement"
    await publish(
        RunEvent(
            type=EventType.STEP_STARTED,
            run_id=run.run_id,
            step_path=path,
            data={"kind": "prompt", "attempt": 1},
        )
    )
    for chunk in ("hello ", "world"):
        rec = StreamRecord(kind="text_delta", text=chunk)
        store.append_stream(run.run_id, path, rec)
        await sink.emit_stream(path, rec)
    record = StepRecord(
        path=path, id="implement", kind="prompt", status=StepStatus.SUCCEEDED, duration_ms=1200
    )
    written = store.record_step(run, record, "hello world")
    assert written is not None
    await publish(
        RunEvent(
            type=EventType.STEP_FINISHED,
            run_id=run.run_id,
            step_path=path,
            data={"status": "succeeded", "duration_ms": 1200},
        )
    )

    # a composite step writes JSON output
    loop_rec = StepRecord(path="build", id="build", kind="loop", status=StepStatus.SUCCEEDED)
    store.record_step(run, loop_rec, json.dumps({"iterations": 1, "converged": True}), kind="json")

    run.status = RunStatus.SUCCEEDED
    store.save(run)
    await publish(
        RunEvent(type=EventType.RUN_FINISHED, run_id=run.run_id, data={"status": "succeeded"})
    )
    await sink.aclose()

    # -- resume view: everything needed to rebuild the reuse cache is on disk ---------------
    loaded = store.load(store.resolve_run_id(run.run_id[:12]))
    assert loaded.status is RunStatus.SUCCEEDED
    step = loaded.steps[path]
    assert step.reusable and step.output_ref == "steps/build[1]/implement/output.txt"
    assert (store.run_dir(run.run_id) / step.output_ref).exists()
    assert store.read_output(run.run_id, step.output_ref) == "hello world"
    assert json.loads(store.read_output(run.run_id, loaded.steps["build"].output_ref or "")) == {
        "iterations": 1,
        "converged": True,
    }
    assert [e.type for e in store.read_events(run.run_id)] == [
        EventType.RUN_STARTED,
        EventType.STEP_STARTED,
        EventType.STEP_FINISHED,
        EventType.RUN_FINISHED,
    ]
    assert [r.text for r in store.read_stream(run.run_id, path)] == ["hello ", "world"]
    assert [r.run_id for r in store.list_runs()] == [run.run_id]

    # -- observers saw the same thing ------------------------------------------------------
    assert [e.type for e in collected.events] == [e.type for e in store.read_events(run.run_id)]
    assert collected.stream_for(path) == list(store.read_stream(run.run_id, path))
    json_lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [line["type"] for line in json_lines] == [
        "run.started",
        "step.started",
        "stream",
        "stream",
        "step.finished",
        "run.finished",
    ]
    text = console.export_text()
    assert "→ build[1]/implement" not in text, "quiet mode: one line per step finish"
    assert "✓ build[1]/implement succeeded 1.2s" in text
    assert f"■ run {run.run_id} succeeded" in text
