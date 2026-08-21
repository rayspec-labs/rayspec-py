# SPDX-License-Identifier: Apache-2.0
"""The ValidatingSink test double itself: it forwards, and it fails on a shape violation."""

from __future__ import annotations

import json

import pytest

from rayspec.events import CollectingSink, EventSink, EventType, RunEvent, StreamRecord

from ._validating import SchemaViolation, ValidatingSink

pytestmark = pytest.mark.anyio

RUN = "20260821-101500-ab2c"


async def test_it_is_a_sink_and_forwards_to_the_wrapped_one() -> None:
    inner = CollectingSink()
    sink = ValidatingSink(inner)
    assert isinstance(sink, EventSink)
    event = RunEvent(type=EventType.RUN_STARTED, run_id=RUN, data={"workflow": "t"})
    record = StreamRecord(kind="stdout", text="hi", attempt=1)
    await sink.emit(event)
    await sink.emit_stream("build/implement", record)
    await sink.aclose()
    assert inner.events == [event]
    assert inner.streams == [("build/implement", record)]
    assert sink.events_of(EventType.RUN_STARTED) == [event]  # a CollectingSink in its own right
    assert sink.stream_for("build/implement") == [record]
    assert inner.closed and sink.closed


class _WrongShape(StreamRecord):
    """A record whose serialization drifted away from the published schema."""

    def to_json(self) -> str:
        return json.dumps({"kind": "stdout", "attempt": "one", "text": "hi"})


async def test_a_record_that_breaks_the_published_shape_raises() -> None:
    sink = ValidatingSink(CollectingSink())
    with pytest.raises(SchemaViolation) as excinfo:
        await sink.emit_stream("a", _WrongShape(kind="stdout"))
    assert "attempt" in str(excinfo.value)
    assert sink.streams == []  # nothing is collected or forwarded


async def test_no_inner_sink_is_fine() -> None:
    sink = ValidatingSink()
    event = RunEvent(type=EventType.RUN_FINISHED, run_id=RUN, data={"status": "succeeded"})
    await sink.emit(event)
    await sink.aclose()
    assert sink.events == [event]


class _BadTimestamp(RunEvent):
    """An event whose `ts` stopped being the ISO-8601 timestamp the schema declares."""

    def to_json(self) -> str:
        payload = json.loads(super().to_json())
        payload["ts"] = "yesterday"
        return json.dumps(payload)


async def test_a_timestamp_that_is_not_a_date_time_raises() -> None:
    """`format: date-time` is not asserted by jsonschema alone — the sink checks it itself."""
    sink = ValidatingSink()
    event = _BadTimestamp(type=EventType.RUN_STARTED, run_id=RUN)
    with pytest.raises(SchemaViolation) as excinfo:
        await sink.emit(event)
    assert "ts" in str(excinfo.value)
    assert sink.events == []
