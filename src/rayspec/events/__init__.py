# SPDX-License-Identifier: Apache-2.0
"""Run lifecycle events, per-step stream records, the sink protocol and the built-in sinks.

Exports only — see :mod:`rayspec.events.model`, :mod:`rayspec.events.base` and
:mod:`rayspec.events.sinks`. The sinks are imported lazily (module ``__getattr__``) so that
importing the models (e.g. via ``rayspec.store.model``) does not load ``rich``.

The discovery helpers of :mod:`rayspec.registry` — where sinks are registered (the builtins plus
whatever is installed under the ``rayspec.sinks`` entry-point group) — are re-exported the same
lazy way: ``create_sink``, ``list_sinks``, ``register_sink``, ``SinkContext``,
``SinkRegistration``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rayspec.events.base import EventSink
from rayspec.events.model import EventType, RunEvent, StreamRecord

if TYPE_CHECKING:
    from rayspec.events.sinks import (
        CollectingSink,
        ConsoleSink,
        JsonStdoutSink,
        MultiSink,
        NullSink,
        QuietConsoleSink,
    )
    from rayspec.registry import (
        SinkContext,
        SinkRegistration,
        create_sink,
        list_sinks,
        register_sink,
    )

#: Resolved from :mod:`rayspec.registry` on first use (it must not be imported eagerly either).
_REGISTRY = frozenset(
    {"SinkContext", "SinkRegistration", "create_sink", "list_sinks", "register_sink"}
)

_SINKS = frozenset(
    {
        "CollectingSink",
        "ConsoleSink",
        "JsonStdoutSink",
        "MultiSink",
        "NullSink",
        "QuietConsoleSink",
    }
)


def __getattr__(name: str) -> object:
    """Resolve the sink classes on first use (keeps ``rich`` out of the model import path)."""
    if name in _SINKS:
        from rayspec.events import sinks

        return getattr(sinks, name)
    if name in _REGISTRY:
        from rayspec import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CollectingSink",
    "ConsoleSink",
    "EventSink",
    "EventType",
    "JsonStdoutSink",
    "MultiSink",
    "NullSink",
    "QuietConsoleSink",
    "RunEvent",
    "SinkContext",
    "SinkRegistration",
    "StreamRecord",
    "create_sink",
    "list_sinks",
    "register_sink",
]
