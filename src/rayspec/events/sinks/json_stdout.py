# SPDX-License-Identifier: Apache-2.0
"""JsonStdoutSink — ``--json`` mode: every event and stream record as one JSON line."""

from __future__ import annotations

import json
from typing import TextIO

from rayspec.events.model import RunEvent, StreamRecord
from rayspec.events.sinks._log import log


class JsonStdoutSink:
    """Write JSON lines to a text stream (stdout, a file, a pipe).

    Lifecycle events are written as ``RunEvent.to_json()``; stream records are wrapped so a
    consumer can discriminate on ``type``::

        {"type": "stream", "step_path": "build[1]/implement", "record": {...StreamRecord...}}

    Each line is flushed immediately. If the stream cannot encode a line (non-UTF-8 stdout, e.g.
    ``PYTHONIOENCODING=ascii`` or Windows pipes) the line is re-emitted as ASCII-escaped JSON
    instead of being dropped. IO errors are logged once and otherwise ignored — an observer must
    never take the engine down. The stream is only closed on :meth:`aclose` when
    ``close_stream=True`` (``sys.stdout`` is not ours to close).
    """

    def __init__(self, stream: TextIO, *, close_stream: bool = False) -> None:
        self.stream = stream
        self.close_stream = close_stream
        self._failed = False

    async def emit(self, event: RunEvent) -> None:
        """Write ``event`` as one JSON line."""
        self._write_line(event.to_json())

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        """Write ``record`` wrapped in ``{"type": "stream", "step_path": ..., "record": ...}``."""
        payload = {
            "type": "stream",
            "step_path": step_path,
            "record": record.model_dump(mode="json"),
        }
        self._write_line(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def aclose(self) -> None:
        """Flush (and close the stream if we own it)."""
        try:
            self.stream.flush()
            if self.close_stream:
                self.stream.close()
        except (OSError, ValueError) as exc:
            self._report(exc)

    def _write_line(self, line: str) -> None:
        try:
            try:
                self.stream.write(line + "\n")
            except UnicodeEncodeError:
                self.stream.write(_ascii_json(line) + "\n")
            self.stream.flush()
        except (OSError, ValueError) as exc:  # ValueError: I/O on closed file
            self._report(exc)

    def _report(self, exc: BaseException) -> None:
        if not self._failed:
            self._failed = True
            log.warning("json sink: cannot write to %r: %s", self.stream, exc)


def _ascii_json(line: str) -> str:
    """Re-serialise a JSON line with ``ensure_ascii`` (``\\uXXXX`` escapes, same payload)."""
    return json.dumps(json.loads(line), ensure_ascii=True, separators=(",", ":"))


__all__ = ["JsonStdoutSink"]
