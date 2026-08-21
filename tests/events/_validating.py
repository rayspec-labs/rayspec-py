# SPDX-License-Identifier: Apache-2.0
"""ValidatingSink: assert every emitted event/stream line against the published schemas.

Test helper (underscore → not collected). Wrap any :class:`~rayspec.events.base.EventSink` with
it — or drop it in where a :class:`~rayspec.events.sinks.CollectingSink` is used, which it
subclasses so ``events_of`` / ``stream_for`` keep working — and the test fails the moment an
event or stream record stops matching ``schemas/events.schema.json`` /
``schemas/stream.schema.json``, the shapes other tools consume. ``format: date-time`` is not
asserted by jsonschema without an extra package, so timestamps are checked explicitly against
:meth:`datetime.fromisoformat` — they are one of the fields most likely to drift. The schemas come from
:mod:`rayspec.schemagen` (the same documents the checked-in files hold), so the sink also proves
that generated schema and live model agree.

Unlike a real sink this one **raises**: an observer must never break a run, but a test must.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from jsonschema import Draft202012Validator

from rayspec.events.base import EventSink
from rayspec.events.model import RunEvent, StreamRecord
from rayspec.events.sinks import CollectingSink
from rayspec.schemagen import build_schema


def _datetime_properties(schema: dict[str, Any]) -> tuple[str, ...]:
    """Top-level properties the schema declares as ``format: date-time``."""
    properties = schema.get("properties", {})
    return tuple(
        name
        for name, sub in properties.items()
        if isinstance(sub, dict) and sub.get("format") == "date-time"
    )


class SchemaViolation(AssertionError):
    """An event or stream record that does not match its published schema."""


class ValidatingSink(CollectingSink):
    """Validate everything that passes through, collect it, then forward it to ``inner``."""

    def __init__(self, inner: EventSink | None = None) -> None:
        super().__init__()
        self.inner = inner
        events, stream = build_schema("events"), build_schema("stream")
        # FORMAT_CHECKER asserts whatever formats the installed jsonschema extras cover; the
        # date-time ones need an extra package, so they are checked explicitly below.
        self._event_validator = Draft202012Validator(
            events, format_checker=Draft202012Validator.FORMAT_CHECKER
        )
        self._stream_validator = Draft202012Validator(
            stream, format_checker=Draft202012Validator.FORMAT_CHECKER
        )
        self._event_datetimes = _datetime_properties(events)
        self._stream_datetimes = _datetime_properties(stream)

    def _check(
        self, validator: Draft202012Validator, datetimes: tuple[str, ...], line: str, what: str
    ) -> None:
        payload: Any = json.loads(line)
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        if errors:
            details = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:3])
            raise SchemaViolation(f"{what} does not match its schema: {details}\nline: {line}")
        if not isinstance(payload, dict):
            return
        for name in datetimes:
            value = payload.get(name)
            if value is None:
                continue
            try:
                datetime.fromisoformat(str(value))
            except ValueError:
                raise SchemaViolation(
                    f"{what}: {name!r} is not the ISO-8601 timestamp the schema declares: "
                    f"{value!r}\nline: {line}"
                ) from None

    async def emit(self, event: RunEvent) -> None:
        self._check(
            self._event_validator,
            self._event_datetimes,
            event.to_json(),
            f"event {event.type.value}",
        )
        await super().emit(event)
        if self.inner is not None:
            await self.inner.emit(event)

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        self._check(
            self._stream_validator,
            self._stream_datetimes,
            record.to_json(),
            f"stream record {record.kind!r}",
        )
        await super().emit_stream(step_path, record)
        if self.inner is not None:
            await self.inner.emit_stream(step_path, record)

    async def aclose(self) -> None:
        await super().aclose()
        if self.inner is not None:
            await self.inner.aclose()


__all__ = ["SchemaViolation", "ValidatingSink"]
