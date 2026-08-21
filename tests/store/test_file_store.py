"""FileRunStore: layout, atomic save, write-ahead order, outputs, listing, prefix resolution."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio import to_thread

from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.providers.base import Usage
from rayspec.schema import RunStatus, StepStatus
from rayspec.store import FileRunStore, RunStore
from rayspec.store.file import (
    AmbiguousRunIdError,
    CorruptRunError,
    RunExistsError,
    StoreError,
    UnknownRunIdError,
    WrittenOutput,
)
from rayspec.store.model import ErrorInfo, RunRecord, SessionRef, StepRecord


def make_run(run_id: str = "20260820-101500-ab2c", **kw: Any) -> RunRecord:
    run = RunRecord(
        run_id=run_id,
        workflow_name="fix_issue",
        workflow_path="/repo/.rayspec/workflows/fix_issue.yaml",
        workflow_hash="sha256:abc",
        project_slug="github.com/o/r",
        project_root="/repo",
        inputs={"issue": 12},
    )
    return run.model_copy(update=kw) if kw else run


@pytest.fixture
def store(tmp_path: Path) -> FileRunStore:
    return FileRunStore(tmp_path)


# -- protocol + layout ------------------------------------------------------------------------


def test_file_store_satisfies_protocol(store: FileRunStore):
    assert isinstance(store, RunStore)


def test_create_lays_out_run_dir(store: FileRunStore, tmp_path: Path):
    run = make_run()
    store.create(run)
    run_dir = tmp_path / "runs" / run.run_id
    assert store.run_dir(run.run_id) == run_dir
    assert (run_dir / "run.json").is_file()
    assert (run_dir / "steps").is_dir()
    assert (run_dir / "artifacts").is_dir()
    assert (run_dir / "tmp").is_dir()
    assert not list(run_dir.glob("*.tmp"))


def test_create_twice_raises(store: FileRunStore):
    run = make_run()
    store.create(run)
    with pytest.raises(RunExistsError):
        store.create(run)
    assert issubclass(RunExistsError, StoreError)


def test_step_dir_for_nested_paths(store: FileRunStore):
    run = make_run()
    store.create(run)
    d = store.step_dir(run.run_id, "build[2]/implement")
    assert d == store.run_dir(run.run_id) / "steps" / "build[2]" / "implement"
    assert d.is_dir()
    with pytest.raises(ValueError):
        store.step_dir(run.run_id, "../escape")


def test_run_id_is_validated(store: FileRunStore):
    with pytest.raises(ValueError):
        store.run_dir("../etc")
    with pytest.raises(ValueError):
        store.run_dir("a/b")


# -- save / load ------------------------------------------------------------------------------


def test_roundtrip_with_nested_steps_and_datetimes(store: FileRunStore):
    run = make_run(status=RunStatus.SUCCEEDED)
    run.started_at = datetime(2026, 8, 20, 10, 15, 0, tzinfo=UTC)
    run.steps["assess"] = StepRecord(
        path="assess",
        id="assess",
        kind="prompt",
        status=StepStatus.SUCCEEDED,
        started_at=datetime(2026, 8, 20, 10, 15, 1, tzinfo=UTC),
        ended_at=datetime(2026, 8, 20, 10, 15, 9, 500000, tzinfo=UTC),
        duration_ms=8500,
        usage=Usage(input=100, output=20),
        cost_usd=0.01,
        session_ref=SessionRef(provider="claude", id="s1"),
        output_ref="steps/assess/output.txt",
        output_kind="text",
    )
    run.steps["build[2]/implement"] = StepRecord(
        path="build[2]/implement",
        id="implement",
        kind="shell",
        status=StepStatus.FAILED,
        iteration=2,
        error=ErrorInfo(type="ShellError", message="exit 1"),
        tolerated=True,
    )
    store.create(run)
    loaded = store.load(run.run_id)
    assert loaded == run
    started = loaded.steps["assess"].started_at
    assert started is not None and started.tzinfo is not None
    raw = json.loads((store.run_dir(run.run_id) / "run.json").read_text())
    assert raw["schema"] == 1 and "build[2]/implement" in raw["steps"]


def test_load_ignores_unknown_keys(store: FileRunStore):
    run = make_run()
    store.create(run)
    path = store.run_dir(run.run_id) / "run.json"
    raw = json.loads(path.read_text())
    raw["future_field"] = {"x": 1}
    raw["steps"]["a"] = {"path": "a", "id": "a", "kind": "shell", "extra": True}
    path.write_text(json.dumps(raw))
    loaded = store.load(run.run_id)
    assert loaded.run_id == run.run_id and "a" in loaded.steps


def test_load_unknown_run_raises(store: FileRunStore):
    with pytest.raises(UnknownRunIdError):
        store.load("20260820-101500-zzzz")


def test_save_is_atomic_and_leaves_no_tmp(store: FileRunStore):
    run = make_run()
    store.create(run)
    run.status = RunStatus.FAILED
    store.save(run)
    run_dir = store.run_dir(run.run_id)
    assert not list(run_dir.glob("run.json.*")), "tmp file must not linger"
    assert store.load(run.run_id).status is RunStatus.FAILED


def test_save_crash_keeps_old_run_json(store: FileRunStore, monkeypatch: pytest.MonkeyPatch):
    run = make_run()
    store.create(run)
    before = (store.run_dir(run.run_id) / "run.json").read_bytes()

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(os, "replace", boom)
    run.status = RunStatus.FAILED
    with pytest.raises(OSError):
        store.save(run)
    monkeypatch.undo()
    assert (store.run_dir(run.run_id) / "run.json").read_bytes() == before
    assert store.load(run.run_id).status is RunStatus.RUNNING
    assert not list(store.run_dir(run.run_id).glob("run.json.*")), "tmp must be cleaned up"


def test_save_tmp_file_uses_pid_and_counter(store: FileRunStore, monkeypatch: pytest.MonkeyPatch):
    run = make_run()
    store.create(run)
    seen: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(os.path.basename(src))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    store.save(run)
    store.save(run)
    assert len(seen) == 2 and seen[0] != seen[1]
    for name in seen:
        assert name.startswith(f"run.json.{os.getpid()}.") and name.endswith(".tmp")


def test_save_concurrently_from_threads_never_corrupts(store: FileRunStore):
    run = make_run()
    store.create(run)

    async def main():
        async def worker(n: int):
            for i in range(10):
                r = make_run(status=RunStatus.RUNNING)
                r.steps[f"s{n}_{i}"] = StepRecord(path=f"s{n}_{i}", id="s", kind="shell")
                await to_thread.run_sync(store.save, r)

        async with anyio.create_task_group() as tg:
            for n in range(4):
                tg.start_soon(worker, n)

    anyio.run(main, backend="asyncio")
    loaded = store.load(run.run_id)
    assert len(loaded.steps) == 1
    assert not list(store.run_dir(run.run_id).glob("run.json.*"))


# -- outputs ----------------------------------------------------------------------------------


def test_write_output_text(store: FileRunStore):
    run = make_run()
    store.create(run)
    ref = store.write_output(run.run_id, "assess", "hello\nworld", kind="text")
    assert ref == "steps/assess/output.txt"
    assert (store.run_dir(run.run_id) / ref).read_text() == "hello\nworld"
    assert store.read_output(run.run_id, ref) == "hello\nworld"


def test_write_output_json_is_pretty(store: FileRunStore):
    run = make_run()
    store.create(run)
    ref = store.write_output(run.run_id, "plan", '{"b":[1,2],"a":"x"}', kind="json")
    assert ref == "steps/plan/output.json"
    text = (store.run_dir(run.run_id) / ref).read_text()
    assert text == '{\n  "b": [\n    1,\n    2\n  ],\n  "a": "x"\n}\n'
    assert json.loads(store.read_output(run.run_id, ref)) == {"b": [1, 2], "a": "x"}


def test_write_output_json_rejects_invalid_json(store: FileRunStore):
    run = make_run()
    store.create(run)
    with pytest.raises(ValueError):
        store.write_output(run.run_id, "plan", "not json", kind="json")


def test_write_output_rejects_unknown_kind(store: FileRunStore):
    run = make_run()
    store.create(run)
    with pytest.raises(ValueError):
        store.write_output(run.run_id, "plan", "x", kind="yaml")


def test_write_output_replaces_stale_other_kind(store: FileRunStore):
    run = make_run()
    store.create(run)
    store.write_output(run.run_id, "s", "text", kind="text")
    store.write_output(run.run_id, "s", "{}", kind="json")
    d = store.step_dir(run.run_id, "s")
    assert (d / "output.json").exists() and not (d / "output.txt").exists()


def test_write_output_with_sha_returns_dataclass(store: FileRunStore):
    run = make_run()
    store.create(run)
    content = "héllo" * 10
    out = store.write_output_with_sha(run.run_id, "build[1]/implement", content, kind="text")
    assert isinstance(out, WrittenOutput)
    assert out.output_ref == "steps/build[1]/implement/output.txt"
    assert out.kind == "text"
    assert out.sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert out.size == len(content.encode("utf-8"))
    assert out.path == store.run_dir(run.run_id) / out.output_ref


def test_write_output_with_sha_json_hashes_pretty_form(store: FileRunStore):
    run = make_run()
    store.create(run)
    out = store.write_output_with_sha(run.run_id, "p", '{"a":1}', kind="json")
    written = (store.run_dir(run.run_id) / out.output_ref).read_bytes()
    assert out.sha256 == hashlib.sha256(written).hexdigest()
    assert out.size == len(written)


def test_large_output_streams_in_chunks(store: FileRunStore, monkeypatch: pytest.MonkeyPatch):
    """Tens of MB must be written chunk-wise (no second full-size copy in memory)."""
    import rayspec.store.file as file_mod

    monkeypatch.setattr(file_mod, "_CHUNK_CHARS", 1 << 16)
    run = make_run()
    store.create(run)
    content = ("x" * 1023 + "\n") * (20 * 1024)  # 20 MiB
    out = store.write_output_with_sha(run.run_id, "big", content, kind="text")
    path = store.run_dir(run.run_id) / out.output_ref
    assert path.stat().st_size == len(content)
    assert out.sha256 == hashlib.sha256(content.encode()).hexdigest()
    assert store.read_output(run.run_id, out.output_ref) == content


def test_read_output_refuses_escape(store: FileRunStore):
    run = make_run()
    store.create(run)
    with pytest.raises(ValueError):
        store.read_output(run.run_id, "../other/run.json")
    with pytest.raises(ValueError):
        store.read_output(run.run_id, "/etc/passwd")


def test_read_output_missing_raises_file_not_found(store: FileRunStore):
    run = make_run()
    store.create(run)
    with pytest.raises(FileNotFoundError):
        store.read_output(run.run_id, "steps/nope/output.txt")


# -- write-ahead helper -----------------------------------------------------------------------


def test_record_step_writes_output_before_run_json(
    store: FileRunStore, monkeypatch: pytest.MonkeyPatch
):
    run = make_run()
    store.create(run)
    order: list[str] = []
    real_write = store.write_output_with_sha
    real_save = store.save

    def spy_write(*a, **kw):
        order.append("output")
        return real_write(*a, **kw)

    def spy_save(r):
        order.append("run.json")
        return real_save(r)

    monkeypatch.setattr(store, "write_output_with_sha", spy_write)
    monkeypatch.setattr(store, "save", spy_save)
    rec = StepRecord(path="assess", id="assess", kind="prompt", status=StepStatus.SUCCEEDED)
    out = store.record_step(run, rec, "result text")
    assert order == ["output", "run.json"]
    assert out is not None and out.output_ref == "steps/assess/output.txt"
    assert rec.output_ref == out.output_ref
    assert rec.output_kind == "text"
    assert rec.output_sha256 == hashlib.sha256(b"result text").hexdigest()
    assert rec.reusable
    loaded = store.load(run.run_id)
    assert loaded.steps["assess"].output_ref == "steps/assess/output.txt"
    assert run.steps["assess"] is rec


def test_record_step_json_kind_and_no_output(store: FileRunStore):
    run = make_run()
    store.create(run)
    rec = StepRecord(path="plan", id="plan", kind="prompt", status=StepStatus.SUCCEEDED)
    out = store.record_step(run, rec, '{"a": 1}', kind="json")
    assert out is not None and rec.output_kind == "json"
    assert rec.output_ref is not None and rec.output_ref.endswith("output.json")
    failed = StepRecord(path="x", id="x", kind="shell", status=StepStatus.FAILED)
    assert store.record_step(run, failed, None) is None
    assert failed.output_ref is None and not failed.reusable
    assert set(store.load(run.run_id).steps) == {"plan", "x"}


def test_record_step_crash_in_save_keeps_output_file(
    store: FileRunStore, monkeypatch: pytest.MonkeyPatch
):
    run = make_run()
    store.create(run)
    real_replace = os.replace

    def replace_but_crash_on_run_json(src, dst):
        if Path(dst).name == "run.json":
            raise OSError("boom")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace_but_crash_on_run_json)
    rec = StepRecord(path="assess", id="assess", kind="prompt", status=StepStatus.SUCCEEDED)
    with pytest.raises(OSError):
        store.record_step(run, rec, "partial")
    monkeypatch.undo()
    # output exists on disk (write-ahead), but run.json does not reference it
    assert (store.step_dir(run.run_id, "assess") / "output.txt").read_text() == "partial"
    assert "assess" not in store.load(run.run_id).steps


# -- events / streams -------------------------------------------------------------------------


def test_append_event_and_stream_write_json_lines(store: FileRunStore):
    run = make_run()
    store.create(run)
    ev1 = RunEvent(type=EventType.RUN_STARTED, run_id=run.run_id)
    ev2 = RunEvent(
        type=EventType.STEP_STARTED, run_id=run.run_id, step_path="a", data={"kind": "shell"}
    )
    store.append_event(run.run_id, ev1)
    store.append_event(run.run_id, ev2)
    lines = (store.run_dir(run.run_id) / "events.jsonl").read_text().splitlines()
    assert [RunEvent.from_json(line) for line in lines] == [ev1, ev2]
    assert list(store.read_events(run.run_id)) == [ev1, ev2]

    rec = StreamRecord(kind="text_delta", text="hi\nthere", attempt=2)
    store.append_stream(run.run_id, "build[2]/implement", rec)
    spath = store.step_dir(run.run_id, "build[2]/implement") / "stream.jsonl"
    lines = spath.read_text().splitlines()
    assert len(lines) == 1 and StreamRecord.from_json(lines[0]) == rec
    assert list(store.read_stream(run.run_id, "build[2]/implement")) == [rec]
    assert list(store.read_stream(run.run_id, "missing")) == []


def test_read_events_for_unknown_run_is_empty(store: FileRunStore):
    run = make_run()
    store.create(run)
    assert list(store.read_events(run.run_id)) == []


@pytest.mark.anyio
async def test_concurrent_appends_do_not_interleave(store: FileRunStore):
    run = make_run()
    store.create(run)
    n = 200

    async def worker(tag: str):
        for i in range(n):
            rec = StreamRecord(kind="text_delta", text=f"{tag}-{i}-" + ("z" * 2000))
            await to_thread.run_sync(store.append_stream, run.run_id, "s", rec)

    async with anyio.create_task_group() as tg:
        tg.start_soon(worker, "a")
        tg.start_soon(worker, "b")

    lines = (store.step_dir(run.run_id, "s") / "stream.jsonl").read_text().splitlines()
    assert len(lines) == 2 * n
    tags = {"a": set(), "b": set()}
    for line in lines:
        rec = StreamRecord.from_json(line)  # raises if a line were interleaved/corrupt
        tag, idx, _ = rec.text.split("-", 2)
        tags[tag].add(int(idx))
    assert tags["a"] == set(range(n)) and tags["b"] == set(range(n))


# -- listing / resolution / deletion ----------------------------------------------------------


def test_list_runs_newest_first_with_limit(store: FileRunStore):
    ids = ["20260820-101500-aaaa", "20260821-000000-bbbb", "20260819-235959-cccc"]
    for rid in ids:
        store.create(make_run(rid))
    listed = [r.run_id for r in store.list_runs()]
    assert listed == sorted(ids, reverse=True)
    assert [r.run_id for r in store.list_runs(limit=2)] == sorted(ids, reverse=True)[:2]
    assert store.list_run_ids() == sorted(ids, reverse=True)
    assert store.list_runs(limit=0) == []


def test_list_runs_skips_dirs_without_run_json_and_corrupt(store: FileRunStore, tmp_path: Path):
    store.create(make_run("20260820-101500-aaaa"))
    (tmp_path / "runs" / "20260820-101500-bbbb").mkdir(parents=True)
    corrupt = tmp_path / "runs" / "20260820-101500-cccc"
    corrupt.mkdir()
    (corrupt / "run.json").write_text("{not json")
    assert [r.run_id for r in store.list_runs()] == ["20260820-101500-aaaa"]
    assert store.list_run_ids() == ["20260820-101500-cccc", "20260820-101500-aaaa"]


def test_list_runs_on_empty_root(tmp_path: Path):
    store = FileRunStore(tmp_path / "does-not-exist-yet")
    assert store.list_runs() == [] and store.list_run_ids() == []


def test_resolve_run_id_prefix_rules(store: FileRunStore):
    store.create(make_run("20260820-101500-aaaa"))
    store.create(make_run("20260820-101500-aaab"))
    store.create(make_run("20260821-000000-bbbb"))
    assert store.resolve_run_id("20260821") == "20260821-000000-bbbb"
    assert store.resolve_run_id("20260820-101500-aaaa") == "20260820-101500-aaaa"  # exact wins
    with pytest.raises(AmbiguousRunIdError) as ai:
        store.resolve_run_id("20260820-101500-aaa")
    assert "20260820-101500-aaaa" in str(ai.value) and "20260820-101500-aaab" in str(ai.value)
    with pytest.raises(UnknownRunIdError):
        store.resolve_run_id("2027")
    with pytest.raises(UnknownRunIdError):
        store.resolve_run_id("")
    assert issubclass(UnknownRunIdError, StoreError) and issubclass(AmbiguousRunIdError, StoreError)


def test_exists_and_delete_run(store: FileRunStore):
    run = make_run()
    assert not store.exists(run.run_id)
    store.create(run)
    store.write_output(run.run_id, "a", "x", kind="text")
    assert store.exists(run.run_id)
    store.delete_run(run.run_id)
    assert not store.exists(run.run_id)
    assert not store.run_dir(run.run_id).exists()
    with pytest.raises(UnknownRunIdError):
        store.delete_run(run.run_id)


# -- review follow-ups ------------------------------------------------------------------------


def test_load_corrupt_run_json_raises_store_error(store: FileRunStore, tmp_path: Path):
    run_id = "20260820-101500-cccc"
    corrupt = tmp_path / "runs" / run_id
    corrupt.mkdir(parents=True)
    (corrupt / "run.json").write_text("{not json")
    with pytest.raises(CorruptRunError) as info:
        store.load(run_id)
    assert isinstance(info.value, StoreError)
    assert run_id in str(info.value) and "run.json" in str(info.value)
    # invalid record (valid JSON, wrong shape) and non-UTF-8 bytes are reported the same way
    (corrupt / "run.json").write_text('{"run_id": 1}')
    with pytest.raises(CorruptRunError):
        store.load(run_id)
    (corrupt / "run.json").write_bytes(b"\xff\xfe{")
    with pytest.raises(CorruptRunError):
        store.load(run_id)
    # listing still skips it
    assert store.list_runs() == []


def test_write_output_failure_keeps_previous_output(
    store: FileRunStore, monkeypatch: pytest.MonkeyPatch
):
    run = make_run()
    store.create(run)
    ref = store.write_output(run.run_id, "a", "good output", kind="text")
    path = store.run_dir(run.run_id) / ref
    # a lone surrogate cannot be encoded: the write fails half-way through
    with pytest.raises(UnicodeEncodeError):
        store.write_output(run.run_id, "a", "bad \udc80 output", kind="text")
    assert path.read_text(encoding="utf-8") == "good output"
    assert not list(path.parent.glob("*.tmp")), "tmp output must be cleaned up"

    def boom(fd):
        raise OSError("simulated ENOSPC on fsync")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        store.write_output(run.run_id, "a", "newer output", kind="text")
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == "good output"
    assert not list(path.parent.glob("*.tmp"))
    # and the store still works afterwards
    store.write_output(run.run_id, "a", "newest", kind="text")
    assert store.read_output(run.run_id, ref) == "newest"
    assert sorted(p.name for p in path.parent.iterdir()) == ["output.txt"]


def test_read_events_and_stream_tolerate_torn_trailing_line(
    store: FileRunStore, caplog: pytest.LogCaptureFixture
):
    run = make_run()
    store.create(run)
    store.append_event(run.run_id, RunEvent(type=EventType.RUN_STARTED, run_id=run.run_id))
    store.append_stream(run.run_id, "a", StreamRecord(kind="stdout", text="hi"))
    with open(store.run_dir(run.run_id) / "events.jsonl", "a") as fh:
        fh.write('{"type": "step.sta')
    with open(store.step_dir(run.run_id, "a") / "stream.jsonl", "a") as fh:
        fh.write('{"kind": "std')
    with caplog.at_level(logging.WARNING, logger="rayspec.store.file"):
        events = list(store.read_events(run.run_id))
        records = list(store.read_stream(run.run_id, "a"))
    assert [e.type for e in events] == [EventType.RUN_STARTED]
    assert [r.text for r in records] == ["hi"]
    assert sum("torn" in r.getMessage() for r in caplog.records) == 2


def test_read_events_skips_bad_line_in_the_middle(
    store: FileRunStore, caplog: pytest.LogCaptureFixture
):
    run = make_run()
    store.create(run)
    store.append_event(run.run_id, RunEvent(type=EventType.RUN_STARTED, run_id=run.run_id))
    with open(store.run_dir(run.run_id) / "events.jsonl", "a") as fh:
        fh.write("garbage\n")
    store.append_event(run.run_id, RunEvent(type=EventType.RUN_FINISHED, run_id=run.run_id))
    with caplog.at_level(logging.WARNING, logger="rayspec.store.file"):
        events = list(store.read_events(run.run_id))
    assert [e.type for e in events] == [EventType.RUN_STARTED, EventType.RUN_FINISHED]
    assert any("skipping" in r.getMessage() for r in caplog.records)


def test_empty_step_path_is_rejected(store: FileRunStore):
    run = make_run()
    store.create(run)
    with pytest.raises(ValueError, match="empty"):
        store.step_dir(run.run_id, "")
    with pytest.raises(ValueError, match="empty"):
        store.write_output(run.run_id, "", "x", kind="text")
    with pytest.raises(ValueError, match="empty"):
        store.append_stream(run.run_id, "", StreamRecord(kind="stdout", text="x"))
    with pytest.raises(ValueError, match="empty"):
        list(store.read_stream(run.run_id, ""))
    assert not (store.run_dir(run.run_id) / "steps" / "output.txt").exists()
    assert not (store.run_dir(run.run_id) / "steps" / "stream.jsonl").exists()


def test_write_output_json_rejects_nan_and_infinity(store: FileRunStore):
    run = make_run()
    store.create(run)
    for bad in ("NaN", "[Infinity]", '{"x": -Infinity}'):
        with pytest.raises(ValueError, match="not valid JSON"):
            store.write_output(run.run_id, "a", bad, kind="json")
    assert not (store.step_dir(run.run_id, "a") / "output.json").exists()


def test_save_without_create_lays_out_skeleton(store: FileRunStore):
    run = make_run()
    store.save(run)
    run_dir = store.run_dir(run.run_id)
    assert sorted(p.name for p in run_dir.iterdir()) == ["artifacts", "run.json", "steps", "tmp"]
    assert store.load(run.run_id).run_id == run.run_id


def test_read_output_refuses_symlink_escape(store: FileRunStore, tmp_path: Path):
    run = make_run()
    store.create(run)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    link = store.run_dir(run.run_id) / "link.txt"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="escapes"):
        store.read_output(run.run_id, "link.txt")
    # symlinks that stay inside the run dir are fine
    ref = store.write_output(run.run_id, "a", "inside", kind="text")
    inner = store.run_dir(run.run_id) / "inner.txt"
    inner.symlink_to(store.run_dir(run.run_id) / ref)
    assert store.read_output(run.run_id, "inner.txt") == "inside"
