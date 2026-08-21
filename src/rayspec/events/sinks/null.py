# SPDX-License-Identifier: Apache-2.0
"""NullSink — discards everything (the default when no observer is attached)."""

from __future__ import annotations

from rayspec.events.model import RunEvent, StreamRecord


class NullSink:
    """An :class:`~rayspec.events.base.EventSink` that ignores all events and records."""

    async def emit(self, event: RunEvent) -> None:
        """Discard ``event``."""

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        """Discard ``record``."""

    async def aclose(self) -> None:
        """Nothing to release."""


__all__ = ["NullSink"]
