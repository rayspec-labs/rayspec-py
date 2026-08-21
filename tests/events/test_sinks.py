"""Event sinks: JsonStdoutSink, CollectingSink, NullSink, MultiSink (observers, never raise)."""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from rayspec.events import (
    CollectingSink,
    EventSink,
    EventType,
    JsonStdoutSink,
    MultiSink,
    NullSink,
    QuietConsoleSink,
    RunEvent,
    StreamRecord,
)

pytestmark = pytest.mark.anyio

RUN = "20260820-101500-ab2c"


def ev(type_: EventType, step: str | None = None, **data) -> RunEvent:
    return RunEvent(type=type_, run_id=RUN, step_path=step, data=data)


def test_all_sinks_satisfy_protocol():
    for sink in (
        JsonStdoutSink(io.StringIO()),
        CollectingSink(),
        NullSink(),
        MultiSink([NullSink()]),
    ):
        assert isinstance(sink, EventSink)


async def test_json_stdout_sink_writes_events_and_stream_wrappers_as_json_lines():
    buf = io.StringIO()
    sink = JsonStdoutSink(buf)
    e1 = ev(EventType.RUN_STARTED)
    e2 = ev(EventType.STEP_STARTED, "build[1]/implement", kind="prompt", attempt=1)
    rec = StreamRecord(kind="text_delta", text="héllo\nworld", attempt=1)
    await sink.emit(e1)
    await sink.emit_stream("build[1]/implement", rec)
    await sink.emit(e2)
    await sink.aclose()
    lines = buf.getvalue().splitlines()
    assert len(lines) == 3
    assert RunEvent.from_json(lines[0]) == e1
    wrapper = json.loads(lines[1])
    assert wrapper["type"] == "stream" and wrapper["step_path"] == "build[1]/implement"
    assert StreamRecord.model_validate(wrapper["record"]) == rec
    assert "\n" not in lines[1]
    assert RunEvent.from_json(lines[2]) == e2
    assert not buf.closed, "the sink does not own the stream by default"


async def test_json_stdout_sink_to_real_file(tmp_path: Path):
    path = tmp_path / "out.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        sink = JsonStdoutSink(fh)
        await sink.emit(ev(EventType.RUN_FINISHED, status="succeeded"))
        # flushed per line: visible before close
        assert path.read_text().count("\n") == 1
        await sink.aclose()
    assert json.loads(path.read_text())["type"] == "run.finished"


async def test_json_stdout_sink_close_stream_option(tmp_path: Path):
    fh = open(tmp_path / "o.jsonl", "w")  # noqa: SIM115
    sink = JsonStdoutSink(fh, close_stream=True)
    await sink.emit(ev(EventType.RUN_STARTED))
    await sink.aclose()
    assert fh.closed


async def test_json_stdout_sink_never_raises(caplog: pytest.LogCaptureFixture):
    class Broken(io.StringIO):
        def write(self, s: str) -> int:
            raise OSError("broken pipe")

    sink = JsonStdoutSink(Broken())
    with caplog.at_level(logging.WARNING, logger="rayspec.events"):
        await sink.emit(ev(EventType.RUN_STARTED))
        await sink.emit(ev(EventType.RUN_FINISHED))
        await sink.emit_stream("a", StreamRecord(kind="text_delta", text="x"))
        await sink.aclose()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "log once, then keep going quietly"
    assert "broken pipe" in warnings[0].getMessage()


async def test_json_stdout_sink_ascii_stream_keeps_every_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """A non-UTF-8 stdout must not drop lines: fall back to ASCII-escaped JSON per line."""
    path = tmp_path / "out.jsonl"
    with (
        open(path, "w", encoding="ascii") as fh,
        caplog.at_level(logging.WARNING, logger="rayspec.events"),
    ):
        sink = JsonStdoutSink(fh)
        await sink.emit(ev(EventType.WARNING, message="héllo"))
        await sink.emit_stream("a", StreamRecord(kind="text_delta", text="日本"))
        await sink.emit(ev(EventType.RUN_FINISHED, status="succeeded"))
        await sink.aclose()
    lines = [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]
    assert [line["type"] for line in lines] == ["warning", "stream", "run.finished"]
    assert lines[0]["data"]["message"] == "héllo"
    assert lines[1]["record"]["text"] == "日本"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_json_stdout_sink_closed_stream_does_not_raise():
    buf = io.StringIO()
    buf.close()
    sink = JsonStdoutSink(buf)
    await sink.emit(ev(EventType.RUN_STARTED))
    await sink.aclose()


async def test_collecting_sink_records_everything():
    sink = CollectingSink()
    e = ev(EventType.STEP_FINISHED, "a", status="succeeded")
    rec = StreamRecord(kind="stdout", text="x")
    await sink.emit(e)
    await sink.emit_stream("a", rec)
    assert sink.events == [e]
    assert sink.streams == [("a", rec)]
    assert sink.events_of(EventType.STEP_FINISHED) == [e]
    assert sink.events_of(EventType.RUN_STARTED) == []
    assert sink.stream_for("a") == [rec] and sink.stream_for("b") == []
    assert not sink.closed
    await sink.aclose()
    assert sink.closed
    sink.clear()
    assert sink.events == [] and sink.streams == []


async def test_null_sink_is_noop():
    sink = NullSink()
    await sink.emit(ev(EventType.RUN_STARTED))
    await sink.emit_stream("a", StreamRecord(kind="stdout"))
    await sink.aclose()


async def test_multi_sink_fans_out_and_closes_all(caplog: pytest.LogCaptureFixture):
    a, b = CollectingSink(), CollectingSink()

    class Exploding:
        closed = False

        async def emit(self, event: RunEvent) -> None:
            raise RuntimeError("sink bug")

        async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
            raise RuntimeError("sink bug")

        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("close bug")

    bad = Exploding()
    multi = MultiSink([a, bad, b])
    e = ev(EventType.RUN_STARTED)
    rec = StreamRecord(kind="stdout", text="x")
    with caplog.at_level(logging.WARNING, logger="rayspec.events"):
        await multi.emit(e)
        await multi.emit_stream("s", rec)
        await multi.aclose()
    assert a.events == [e] == b.events
    assert a.streams == [("s", rec)] == b.streams
    assert a.closed and b.closed and bad.closed
    assert any("sink bug" in r.getMessage() for r in caplog.records)
    assert any("close bug" in r.getMessage() for r in caplog.records)
    assert list(multi.sinks) == [a, bad, b]


async def test_multi_sink_accepts_varargs_and_empty():
    a = CollectingSink()
    multi = MultiSink(a, NullSink())
    await multi.emit(ev(EventType.RUN_STARTED))
    assert len(a.events) == 1
    empty = MultiSink()
    await empty.emit(ev(EventType.RUN_STARTED))
    await empty.aclose()


def test_multi_sink_flattens_only_iterables_not_sinks():
    class IterableSink(CollectingSink):
        def __iter__(self):  # a sink that happens to be iterable must not be flattened
            raise AssertionError("must not iterate a sink")

    sink = MultiSink(IterableSink(), [NullSink()])
    assert len(sink.sinks) == 2 and isinstance(sink.sinks[0], IterableSink)


def test_importing_models_does_not_import_rich():
    """``rayspec.store.model``/``rayspec.events`` models must stay light: sinks load lazily."""
    code = (
        "import sys; import rayspec.store.model; import rayspec.events; "
        "assert 'rich' not in sys.modules, 'rich imported eagerly'; "
        "from rayspec.events import QuietConsoleSink; assert 'rich' in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_events_package_exports():
    import rayspec.events as events

    for name in (
        "EventType",
        "RunEvent",
        "StreamRecord",
        "EventSink",
        "JsonStdoutSink",
        "CollectingSink",
        "NullSink",
        "MultiSink",
        "QuietConsoleSink",
    ):
        assert name in events.__all__ and hasattr(events, name)
    assert QuietConsoleSink is not None
