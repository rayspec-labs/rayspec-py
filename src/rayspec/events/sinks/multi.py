# SPDX-License-Identifier: Apache-2.0
"""MultiSink — fan-out to several sinks; one misbehaving sink never affects the others."""

from __future__ import annotations

from collections.abc import Iterable

from rayspec.events.base import EventSink
from rayspec.events.model import RunEvent, StreamRecord
from rayspec.events.sinks._log import log


class MultiSink:
    """Forward every call to each wrapped sink, in order, swallowing (and logging) exceptions.

    Accepts either an iterable of sinks or sinks as positional arguments::

        MultiSink([console, collecting])
        MultiSink(console, collecting)
    """

    def __init__(self, *sinks: EventSink | Iterable[EventSink]) -> None:
        flat: list[EventSink] = []
        for item in sinks:
            if isinstance(item, EventSink):
                flat.append(item)
            else:
                flat.extend(item)
        self.sinks: tuple[EventSink, ...] = tuple(flat)

    async def emit(self, event: RunEvent) -> None:
        """Forward ``event`` to every sink."""
        for sink in self.sinks:
            try:
                await sink.emit(event)
            except Exception as exc:
                log.warning("event sink %r failed on emit: %s", sink, exc)

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        """Forward ``record`` to every sink."""
        for sink in self.sinks:
            try:
                await sink.emit_stream(step_path, record)
            except Exception as exc:
                log.warning("event sink %r failed on emit_stream: %s", sink, exc)

    async def aclose(self) -> None:
        """Close every sink, even if an earlier one raised."""
        for sink in self.sinks:
            try:
                await sink.aclose()
            except Exception as exc:
                log.warning("event sink %r failed on aclose: %s", sink, exc)


__all__ = ["MultiSink"]
