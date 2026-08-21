# SPDX-License-Identifier: Apache-2.0
"""The redaction boundary of the store seam: a store that never *receives* a secret.

Module boundary: pure delegation. This module adds no persistence of its own — it applies
:mod:`rayspec.redact` to everything on its way INTO the wrapped store and forwards the rest.

Why it exists: :class:`~rayspec.store.file.FileRunStore` redacts inside itself, which is the
right place for a store rayspec ships and tests. A store that arrives through the
``rayspec.stores`` entry point is code rayspec cannot vouch for, so redaction must not depend on
it doing anything: :func:`rayspec.registry.create_store` wraps every third-party store in this
class, and the secrets stop *here*, one layer above the plugin. The wrapped store is handed
already-redacted records, outputs, prompts, events and stream records — there is nothing left
for it to leak.

The one rule that keeps that true: a store method that WRITES is either implemented here (and
redacts) or unreachable. :attr:`READ_THROUGH` lists the read-only members that are forwarded
untouched; :attr:`WRITE_THROUGH` the optional writers that are forwarded through a redacting
wrapper; anything else raises :class:`AttributeError` naming this rule, so a store write added
later cannot quietly slip past the boundary.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.redact import NULL_REDACTOR, Redactor, StreamRedactor
from rayspec.store.model import RunRecord

#: Read-only members forwarded to the wrapped store unchanged (they never carry a secret in).
READ_THROUGH: frozenset[str] = frozenset(
    {
        "delete_run",
        "exists",
        "list_run_ids",
        "read_events",
        "read_stream",
        "resolve_run_id",
        "root",
        "runs_root",
    }
)

#: Optional writers of the store surface, forwarded through a redacting wrapper. ``hasattr``
#: still answers for the WRAPPED store, so a store without them degrades exactly as before.
WRITE_THROUGH: frozenset[str] = frozenset({"write_output_with_sha", "write_prompt"})


class RedactingStore:
    """Wraps one :class:`~rayspec.store.base.RunStore` so nothing it persists carries a secret.

    Assign :attr:`redactor` the way the CLI assigns it to any store (the run's secrets are only
    known once the inputs are resolved); ``NULL_REDACTOR`` makes every method a plain forward.
    """

    def __init__(self, inner: Any, redactor: Redactor = NULL_REDACTOR) -> None:
        self.inner = inner
        #: The redactor applied to everything on its way into the wrapped store.
        self.redactor: Redactor = redactor
        self._streams: dict[tuple[str, str, str, int], StreamRedactor] = {}

    # -- run.json -------------------------------------------------------------------------

    def create(self, run: RunRecord) -> None:
        """Create the run through the wrapped store, from a redacted copy of ``run``."""
        self.inner.create(self._record(run))

    def save(self, run: RunRecord) -> None:
        """Persist a redacted copy of ``run`` (the caller's record is never modified)."""
        self.inner.save(self._record(run))

    def load(self, run_id: str) -> RunRecord:
        """Load a stored record (reads need no redaction — what is stored is already clean)."""
        return self.inner.load(run_id)

    def list_runs(self, *, limit: int | None = None) -> list[RunRecord]:
        """Every stored record, newest first."""
        return self.inner.list_runs(limit=limit)

    # -- paths ----------------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        """Directory of a run, as the wrapped store reports it."""
        return self.inner.run_dir(run_id)

    def step_dir(self, run_id: str, step_path: str) -> Path:
        """Directory of one step, as the wrapped store reports it."""
        return self.inner.step_dir(run_id, step_path)

    # -- outputs --------------------------------------------------------------------------

    def write_output(self, run_id: str, step_path: str, content: str, *, kind: str) -> str:
        """Write a redacted step output; returns the wrapped store's ``output_ref``."""
        return self.inner.write_output(
            run_id, step_path, self._output(content, kind=kind), kind=kind
        )

    def read_output(self, run_id: str, output_ref: str) -> str:
        """Read a stored output back."""
        return self.inner.read_output(run_id, output_ref)

    # -- events / streams -----------------------------------------------------------------

    def append_event(self, run_id: str, event: RunEvent) -> None:
        """Append a redacted event; a finished step's held-back stream tail is flushed first.

        ``step.finished``/``run.finished``/``run.paused`` are what END a stream, exactly as in
        :class:`~rayspec.store.file.FileRunStore` — the buffer that catches a secret split
        across two deltas is emptied there so no delta can be lost.
        """
        if self._streams:
            if event.type is EventType.STEP_FINISHED and event.step_path:
                self.flush_streams(run_id, event.step_path)
            elif event.type is EventType.RUN_FINISHED or event.type is EventType.RUN_PAUSED:
                self.flush_streams(run_id)
        if self.redactor and event.data:
            event = event.model_copy(update={"data": self.redactor.redact_obj(event.data)})
        self.inner.append_event(run_id, event)

    def append_stream(self, run_id: str, step_path: str, record: StreamRecord) -> None:
        """Append a stream record whose text is redacted across the chunk boundary."""
        if self.redactor:
            record = self._stream_record(run_id, step_path, record)
        self.inner.append_stream(run_id, step_path, record)

    def flush_streams(self, run_id: str, step_path: str | None = None) -> None:
        """Emit every held-back stream tail of ``run_id`` (all steps, or just ``step_path``)."""
        for key in [
            k for k in self._streams if k[0] == run_id and (step_path is None or k[1] == step_path)
        ]:
            tail = self._streams.pop(key).flush()
            if tail:
                self.inner.append_stream(
                    run_id, key[1], StreamRecord(kind=key[2], attempt=key[3], text=tail)
                )

    # -- delegation -----------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Forward the reviewed members; refuse anything else by name.

        A write that is not implemented above would reach the wrapped store unredacted, so it
        is not reachable at all. ``hasattr`` keeps answering for the wrapped store, which is
        what the engine's optional-feature probes (``write_prompt``) expect.
        """
        if name in WRITE_THROUGH:
            return _REDACTING_WRITERS[name](self, getattr(self.inner, name))
        if name in READ_THROUGH:
            return getattr(self.inner, name)
        raise AttributeError(
            f"{name!r} is not exposed through the redacting store boundary "
            f"({type(self.inner).__name__} may implement it): every store write must be "
            "redacted first, so it has to be implemented on RedactingStore"
        )

    # -- internals ------------------------------------------------------------------------

    def _record(self, run: RunRecord) -> RunRecord:
        """A copy of ``run`` with every secret replaced.

        Redacted on the PARSED value for the same reason :meth:`_output` is: an input whose
        secret value is a number (a PIN, a numeric account id) is a bare JSON token, and
        replacing it in the serialised text would leave a document that no longer parses —
        turning a checkpoint write into a run-ending :class:`ValidationError`.
        :meth:`~rayspec.redact.Redactor.redact_dump` is what keeps the copy a valid record: a
        marker cannot land in a field that only holds a number, so re-validating it here cannot
        fail on a coincidence.
        """
        if not self.redactor:
            return run
        return type(run).model_validate(self.redactor.redact_dump(run))

    def _output(self, content: str, *, kind: str) -> str:
        """Redact one output body.

        ``json`` is redacted on the PARSED value, not on the serialised text: a secret that is a
        bare JSON token would otherwise turn a valid document into an invalid one. Content that
        does not parse is passed through untouched — rejecting it is the store's job and its
        error message is the one the engine knows.
        """
        if not self.redactor:
            return content
        if kind != "json":
            return self.redactor.redact(content)
        try:
            parsed = json.loads(content)
        except ValueError:
            return content
        return json.dumps(self.redactor.redact_obj(parsed), ensure_ascii=False)

    def _stream_record(self, run_id: str, step_path: str, record: StreamRecord) -> StreamRecord:
        update: dict[str, Any] = {}
        if record.text:
            key = (run_id, step_path, record.kind, record.attempt)
            stream = self._streams.get(key)
            if stream is None:
                stream = self._streams[key] = StreamRedactor(self.redactor)
            update["text"] = stream.feed(record.text)
        if record.data:
            update["data"] = self.redactor.redact_obj(record.data)
        if record.name:
            update["name"] = self.redactor.redact(record.name)
        return record.model_copy(update=update) if update else record


def _redacting_write_prompt(store: RedactingStore, inner: Callable[..., Any]) -> Callable[..., Any]:
    def write_prompt(run_id: str, step_path: str, text: str) -> Any:
        return inner(run_id, step_path, store.redactor.redact(text) if store.redactor else text)

    return write_prompt


def _redacting_write_output_with_sha(
    store: RedactingStore, inner: Callable[..., Any]
) -> Callable[..., Any]:
    def write_output_with_sha(run_id: str, step_path: str, content: str, *, kind: str) -> Any:
        return inner(run_id, step_path, store._output(content, kind=kind), kind=kind)

    return write_output_with_sha


#: One redacting wrapper per optional writer (see :attr:`WRITE_THROUGH`).
_Wrapper = Callable[["RedactingStore", Callable[..., Any]], Callable[..., Any]]
_REDACTING_WRITERS: dict[str, _Wrapper] = {
    "write_prompt": _redacting_write_prompt,
    "write_output_with_sha": _redacting_write_output_with_sha,
}


__all__ = ["READ_THROUGH", "WRITE_THROUGH", "RedactingStore"]
