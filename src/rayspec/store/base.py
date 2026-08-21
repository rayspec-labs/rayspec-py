# SPDX-License-Identifier: Apache-2.0
"""RunStore protocol — deliberately tiny so other backends (SQLite, remote) can be added later."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from rayspec.events.model import RunEvent, StreamRecord
from rayspec.redact import NULL_REDACTOR, Redactor
from rayspec.store.model import RunRecord


@runtime_checkable
class RunStore(Protocol):
    #: Additive: the redactor every writer of this store applies. A store is built before
    #: the run's secrets are known, so the CLI ASSIGNS the run's redactor to this attribute at
    #: run start; :data:`~rayspec.redact.NULL_REDACTOR` means "nothing to redact". It is part of
    #: the protocol so the one writer that cannot go through the store — the subprocess pump
    #: writing ``stdout.log`` — can read it off the store it was handed, and a rename is a type
    #: error instead of a silent leak.
    redactor: Redactor = NULL_REDACTOR

    def create(self, run: RunRecord) -> None: ...

    def save(self, run: RunRecord) -> None:
        """Atomically persist the whole record (tmp + fsync + replace)."""
        ...

    def load(self, run_id: str) -> RunRecord: ...

    def list_runs(self, *, limit: int | None = None) -> list[RunRecord]: ...

    def run_dir(self, run_id: str) -> Path: ...

    def step_dir(self, run_id: str, step_path: str) -> Path: ...

    def write_output(self, run_id: str, step_path: str, content: str, *, kind: str) -> str:
        """Write a step output file (fsync) and return its ``output_ref`` (run-dir relative)."""
        ...

    def read_output(self, run_id: str, output_ref: str) -> str: ...

    def append_event(self, run_id: str, event: RunEvent) -> None: ...

    def append_stream(self, run_id: str, step_path: str, record: StreamRecord) -> None: ...


__all__ = ["RunStore"]
