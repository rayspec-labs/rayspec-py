"""The sinks (console, ``--json``, collecting) never show a secret value."""

from __future__ import annotations

import io

import pytest

from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.events.sinks import CollectingSink, JsonStdoutSink, QuietConsoleSink
from rayspec.redact import RedactingSink, Redactor

SECRET = "ghp_SECRETTOKEN_ABCDEF"


def _wrap(inner: object) -> RedactingSink:
    return RedactingSink(inner, Redactor.build({"token": SECRET}))


@pytest.mark.anyio
async def test_event_data_is_redacted() -> None:
    inner = CollectingSink()
    sink = _wrap(inner)
    await sink.emit(RunEvent(type=EventType.WARNING, run_id="r", data={"message": f"saw {SECRET}"}))
    assert inner.events[0].data["message"] == "saw [REDACTED:token]"


@pytest.mark.anyio
async def test_stream_text_and_data_are_redacted() -> None:
    inner = CollectingSink()
    sink = _wrap(inner)
    await sink.emit_stream("s", StreamRecord(kind="tool_call", data={"arg": SECRET}))
    await sink.aclose()
    assert inner.streams[0][1].data["arg"] == "[REDACTED:token]"


@pytest.mark.anyio
async def test_a_secret_split_across_two_deltas_is_redacted_before_the_sink() -> None:
    inner = CollectingSink()
    sink = _wrap(inner)
    await sink.emit_stream("s", StreamRecord(kind="text", text=f"a{SECRET[:7]}"))
    await sink.emit_stream("s", StreamRecord(kind="text", text=f"{SECRET[7:]}b"))
    await sink.aclose()
    joined = "".join(rec.text for _path, rec in inner.streams)
    assert SECRET not in joined
    assert joined == "a[REDACTED:token]b"


@pytest.mark.anyio
async def test_step_finished_flushes_the_held_tail_before_the_event() -> None:
    inner = CollectingSink()
    sink = _wrap(inner)
    await sink.emit_stream("s", StreamRecord(kind="text", text="plain-tail"))
    await sink.emit(RunEvent(type=EventType.STEP_FINISHED, run_id="r", step_path="s"))
    joined = "".join(rec.text for _path, rec in inner.streams)
    assert joined == "plain-tail"
    assert inner.events[-1].type is EventType.STEP_FINISHED


@pytest.mark.anyio
async def test_json_stdout_never_prints_the_value() -> None:
    buffer = io.StringIO()
    sink = _wrap(JsonStdoutSink(buffer))
    await sink.emit_stream("s", StreamRecord(kind="text", text=f"{SECRET}\n"))
    await sink.aclose()
    assert SECRET not in buffer.getvalue()
    assert "[REDACTED:token]" in buffer.getvalue()


@pytest.mark.anyio
async def test_delegates_unknown_attributes_to_the_wrapped_sink() -> None:
    from rich.console import Console

    inner = QuietConsoleSink(Console(file=io.StringIO()))
    sink = _wrap(inner)
    assert sink.format_event.__func__ is inner.format_event.__func__
    await sink.aclose()
