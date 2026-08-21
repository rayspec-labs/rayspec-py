# SPDX-License-Identifier: Apache-2.0
"""Event models: lifecycle ``RunEvent`` (events.jsonl), per-step ``StreamRecord`` (stream.jsonl)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from rayspec.providers.base import AgentEvent


def _utcnow() -> datetime:
    return datetime.now(UTC)


class EventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_RESUMED = "run.resumed"
    RUN_PAUSED = "run.paused"
    RUN_DECISION = "run.decision"
    RUN_FINISHED = "run.finished"
    STEP_STARTED = "step.started"
    STEP_RETRY = "step.retry"
    STEP_FINISHED = "step.finished"
    LOOP_ITERATION = "loop.iteration"
    EACH_ITEM = "each.item"
    WORKSPACE_CREATED = "workspace.created"
    WARNING = "warning"


class RunEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: EventType
    run_id: str
    ts: datetime = Field(default_factory=_utcnow)
    step_path: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, line: str) -> Self:
        return cls.model_validate_json(line)


class StreamRecord(BaseModel):
    """One streamed agent/shell event for a step attempt (``steps/<path>/stream.jsonl``)."""

    model_config = ConfigDict(frozen=True)

    kind: str
    ts: datetime = Field(default_factory=_utcnow)
    attempt: int = 1
    text: str = ""
    name: str | None = None
    call_id: str | None = None
    nested: bool = False
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_agent_event(cls, event: AgentEvent, *, attempt: int = 1) -> Self:
        ts = datetime.fromtimestamp(event.ts, UTC) if event.ts else _utcnow()
        return cls(
            kind=event.kind,
            ts=ts,
            attempt=attempt,
            text=event.text,
            name=event.name,
            call_id=event.call_id,
            nested=event.nested,
            data=dict(event.data),
        )

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, line: str) -> Self:
        return cls.model_validate_json(line)


__all__ = ["EventType", "RunEvent", "StreamRecord"]
