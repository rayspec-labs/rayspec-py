# SPDX-License-Identifier: Apache-2.0
"""EventSink protocol: where lifecycle events and per-step stream records go."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from rayspec.events.model import RunEvent, StreamRecord


@runtime_checkable
class EventSink(Protocol):
    async def emit(self, event: RunEvent) -> None: ...

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None: ...

    async def aclose(self) -> None: ...


__all__ = ["EventSink"]
