# SPDX-License-Identifier: Apache-2.0
"""Event sinks — observers of the run (console, ``--json`` stdout, collecting, null, fan-out).

Sinks never persist anything: the :class:`~rayspec.store.base.RunStore` owns ``events.jsonl``
and ``stream.jsonl``. Every sink implements :class:`~rayspec.events.base.EventSink` and never
raises into the engine (IO problems are logged to ``rayspec.events`` and ignored).
"""

from rayspec.events.sinks.collecting import CollectingSink
from rayspec.events.sinks.console import ConsoleSink, QuietConsoleSink
from rayspec.events.sinks.json_stdout import JsonStdoutSink
from rayspec.events.sinks.multi import MultiSink
from rayspec.events.sinks.null import NullSink

__all__ = [
    "CollectingSink",
    "ConsoleSink",
    "JsonStdoutSink",
    "MultiSink",
    "NullSink",
    "QuietConsoleSink",
]
