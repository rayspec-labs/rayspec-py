from __future__ import annotations

import json

from rayspec.events.model import EventType, RunEvent, StreamRecord


def test_event_types_cover_lifecycle():
    values = {e.value for e in EventType}
    for expected in [
        "run.started",
        "run.resumed",
        "run.paused",
        "run.decision",
        "run.finished",
        "step.started",
        "step.retry",
        "step.finished",
        "loop.iteration",
        "each.item",
        "workspace.created",
        "warning",
    ]:
        assert expected in values


def test_run_event_serializes_to_json_line():
    ev = RunEvent(
        type=EventType.STEP_STARTED,
        run_id="r1",
        step_path="build[1]/implement",
        data={"attempt": 1},
    )
    line = ev.to_json()
    obj = json.loads(line)
    assert obj["type"] == "step.started" and obj["step_path"] == "build[1]/implement"
    assert obj["data"] == {"attempt": 1} and isinstance(obj["ts"], str)
    back = RunEvent.from_json(line)
    assert back.type is EventType.STEP_STARTED and back.run_id == "r1"


def test_stream_record_from_agent_event():
    from rayspec.providers.base import AgentEvent

    rec = StreamRecord.from_agent_event(
        AgentEvent(kind="tool_call", name="Bash", call_id="t1", data={"cmd": "ls"}), attempt=2
    )
    obj = json.loads(rec.to_json())
    assert obj["kind"] == "tool_call" and obj["name"] == "Bash" and obj["attempt"] == 2
    assert obj["data"] == {"cmd": "ls"}
