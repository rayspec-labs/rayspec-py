# SPDX-License-Identifier: Apache-2.0
"""CollectingSink — keeps events and stream records in memory (tests, in-process consumers)."""

from __future__ import annotations

from rayspec.events.model import EventType, RunEvent, StreamRecord


class CollectingSink:
    """An :class:`~rayspec.events.base.EventSink` that appends to ``events`` / ``streams``.

    ``streams`` holds ``(step_path, record)`` tuples in emission order; ``closed`` flips to
    ``True`` on :meth:`aclose`.
    """

    def __init__(self) -> None:
        self.events: list[RunEvent] = []
        self.streams: list[tuple[str, StreamRecord]] = []
        self.closed = False

    async def emit(self, event: RunEvent) -> None:
        """Record ``event``."""
        self.events.append(event)

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        """Record ``(step_path, record)``."""
        self.streams.append((step_path, record))

    async def aclose(self) -> None:
        """Mark the sink closed (collected data stays available)."""
        self.closed = True

    def events_of(self, type_: EventType) -> list[RunEvent]:
        """Events of one type, in order."""
        return [e for e in self.events if e.type is type_]

    def stream_for(self, step_path: str) -> list[StreamRecord]:
        """Stream records emitted for one step path, in order."""
        return [rec for path, rec in self.streams if path == step_path]

    def clear(self) -> None:
        """Forget everything collected so far."""
        self.events.clear()
        self.streams.clear()


__all__ = ["CollectingSink"]
