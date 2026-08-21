# SPDX-License-Identifier: Apache-2.0
"""A :class:`~rayspec.store.base.RunStore` that dies at the n-th write.

Boundary: a test double, not shipped. It wraps any store (in practice
:class:`~rayspec.store.file.FileRunStore`) through the protocol, so the engine needs no change to
be crashed — which is the point: the write-ahead order (output file → record → ``run.json``) and
the resume/reuse predicate are promises only if they hold at *every* interleaving, not just at
the ones a hand-written test happens to pick.

A :class:`FaultPoint` names one persistence point (``save``, ``write_output``, ``append_event``,
``append_stream``) and the occurrence to die at: ``before`` or ``after`` the wrapped call lands,
or — for the two line-oriented JSONL writers — ``torn``, which writes only the first half of the
serialised line and then dies. ``torn`` is the only way to exercise the store's own durability
promise that "readers tolerate a torn trailing line after a crash": a fault that fires before or
after a *whole* call can never leave a partial line behind.

Once it fires the store stays dead: every later write raises :class:`StoreCrash` too, so nothing
is persisted after the crash — the closest in-process approximation of ``kill -9``. Reads keep
working (a real crash does not damage the files that were already written).

Used by ``tests/engine/test_resume_faults.py``; :func:`enumerate_points` turns one clean run's
call counts into the crash points to parametrise over.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rayspec.events.model import RunEvent, StreamRecord
from rayspec.store.base import RunStore
from rayspec.store.file import EVENTS_JSONL, STREAM_JSONL
from rayspec.store.model import RunRecord

#: The write methods of the protocol, in write-ahead order.
WRITE_METHODS: tuple[str, ...] = ("write_output", "save", "append_event", "append_stream")

#: The JSONL writers — the only ones a ``torn`` fault point applies to.
LINE_METHODS: frozenset[str] = frozenset({"append_event", "append_stream"})

#: When a fault fires relative to the wrapped call.
WHENS: frozenset[str] = frozenset({"before", "after", "torn"})


class StoreCrash(RuntimeError):
    """The injected crash — as if the process had died at this persistence point."""


@dataclass(frozen=True)
class FaultPoint:
    """Die at the ``n``-th call of ``method``: ``before``/``after`` the write, or mid-line."""

    method: str
    n: int
    when: str = "after"

    def __post_init__(self) -> None:
        if self.method not in WRITE_METHODS:
            raise ValueError(f"unknown persistence point {self.method!r}")
        if self.when not in WHENS:
            raise ValueError(f"when must be one of {sorted(WHENS)}, not {self.when!r}")
        if self.when == "torn" and self.method not in LINE_METHODS:
            raise ValueError(f"a torn write only applies to {sorted(LINE_METHODS)}")

    def __str__(self) -> str:
        return f"{self.method}#{self.n}-{self.when}"


@dataclass
class FaultyStore:
    """``RunStore`` wrapper that counts every write and dies at :attr:`fault`."""

    inner: RunStore
    fault: FaultPoint | None = None
    counts: dict[str, int] = field(default_factory=dict)
    crashed: bool = False
    #: the JSONL file a ``torn`` fault left a half-written line in
    torn_path: Path | None = None

    # -- writes ---------------------------------------------------------------------------

    def _guard(self, method: str, call: Any, *, tear: Any = None) -> Any:
        if self.crashed:
            raise StoreCrash(f"store is dead (crashed at {self.fault})")
        self.counts[method] = self.counts.get(method, 0) + 1
        fires = self.fault is not None and self.fault.method == method
        fires = fires and self.fault is not None and self.fault.n == self.counts[method]
        when = self.fault.when if fires and self.fault is not None else ""
        if when == "before":
            self.crashed = True
            raise StoreCrash(f"crashed before {self.fault}")
        if when == "torn":
            if tear is None:  # pragma: no cover - FaultPoint refuses the combination
                raise ValueError(f"{method} cannot be torn")
            tear()
            self.crashed = True
            raise StoreCrash(f"crashed mid-line at {self.fault}")
        result = call()
        if fires:
            self.crashed = True
            raise StoreCrash(f"crashed after {self.fault}")
        return result

    def _tear(self, path: Path, line: str) -> None:
        """Append the first half of ``line`` with no newline — a write killed in the middle."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line[: max(1, len(line) // 2)])
            handle.flush()
        self.torn_path = path

    def create(self, run: RunRecord) -> None:
        """Not a fault point: without ``run.json`` there is nothing to resume."""
        self.inner.create(run)

    def save(self, run: RunRecord) -> None:
        self._guard("save", lambda: self.inner.save(run))

    def write_output(self, run_id: str, step_path: str, content: str, *, kind: str) -> str:
        return str(
            self._guard(
                "write_output",
                lambda: self.inner.write_output(run_id, step_path, content, kind=kind),
            )
        )

    def write_output_with_sha(self, run_id: str, step_path: str, content: str, *, kind: str) -> Any:
        """The richer writer the engine prefers — the same persistence point as ``write_output``."""
        writer: Any = getattr(self.inner, "write_output_with_sha", None)
        if writer is None:  # pragma: no cover - FileRunStore always has it
            raise AttributeError("wrapped store has no write_output_with_sha")
        return self._guard("write_output", lambda: writer(run_id, step_path, content, kind=kind))

    def append_event(self, run_id: str, event: RunEvent) -> None:
        self._guard(
            "append_event",
            lambda: self.inner.append_event(run_id, event),
            tear=lambda: self._tear(self.inner.run_dir(run_id) / EVENTS_JSONL, event.to_json()),
        )

    def append_stream(self, run_id: str, step_path: str, record: StreamRecord) -> None:
        self._guard(
            "append_stream",
            lambda: self.inner.append_stream(run_id, step_path, record),
            tear=lambda: self._tear(
                self.inner.step_dir(run_id, step_path) / STREAM_JSONL, record.to_json()
            ),
        )

    # -- reads (a crash does not damage what is already on disk) --------------------------

    def load(self, run_id: str) -> RunRecord:
        return self.inner.load(run_id)

    def list_runs(self, *, limit: int | None = None) -> list[RunRecord]:
        return self.inner.list_runs(limit=limit)

    def run_dir(self, run_id: str) -> Path:
        return self.inner.run_dir(run_id)

    def step_dir(self, run_id: str, step_path: str) -> Path:
        return self.inner.step_dir(run_id, step_path)

    def read_output(self, run_id: str, output_ref: str) -> str:
        return self.inner.read_output(run_id, output_ref)


def enumerate_points(counts: dict[str, int], *, per_method: int = 4) -> list[FaultPoint]:
    """Crash points spread over one clean run's call counts.

    Up to ``per_method`` occurrences of each write method, evenly spaced over the range that run
    actually reached, alternating ``after`` and ``before`` so both sides of every write are
    covered at least once, plus one ``torn`` point in the middle of each JSONL writer so a
    half-written line is covered too.
    """
    points: list[FaultPoint] = []
    for method in WRITE_METHODS:
        total = counts.get(method, 0)
        if total <= 0:
            continue
        step = max(1, total // per_method)
        occurrences = sorted({min(total, 1 + i * step) for i in range(per_method)})
        for index, n in enumerate(occurrences):
            points.append(FaultPoint(method, n, "after" if index % 2 == 0 else "before"))
        if method in LINE_METHODS:
            points.append(FaultPoint(method, max(1, total // 2), "torn"))
    return points


__all__ = [
    "LINE_METHODS",
    "WHENS",
    "WRITE_METHODS",
    "FaultPoint",
    "FaultyStore",
    "StoreCrash",
    "enumerate_points",
]
