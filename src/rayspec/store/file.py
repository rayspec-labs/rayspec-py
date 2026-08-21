# SPDX-License-Identifier: Apache-2.0
"""File-based :class:`~rayspec.store.base.RunStore` — the checkpoint on disk.

Module boundary: this is the only place that knows the on-disk layout of a run. The engine
persists through the protocol (``create``/``save``/``write_output``/``append_event``/
``append_stream``); the CLI reads through ``load``/``list_runs``/``read_*``. Event sinks never
touch these files.

Layout::

    <root>/runs/<run-id>/
        run.json                      RunRecord.model_dump_json() — atomically replaced
        events.jsonl                  lifecycle RunEvents (one JSON object per line)
        audit.jsonl                   the local ledger, only with RAYSPEC_AUDIT_LOG=1
        steps/<step-path>/            StepPath.fs_path(); nested like ``build[2]/implement``
            output.txt | output.json  the step output (write-ahead: written before run.json)
            prompt.txt                prompt steps: the rendered prompt handed to the provider
            stream.jsonl              per-step StreamRecords (agent deltas, tool calls, stdout…)
            stdout.log / stderr.log   written directly by the executors
        artifacts/                    user artifacts
        tmp/                          scratch for executors (spill files etc.)

Durability rules: ``save`` writes ``run.json.<pid>.<n>.tmp``, fsyncs, then ``os.replace``s it
(a crash leaves the previous ``run.json`` intact); outputs are written the same way
(``output.<kind>.<pid>.<n>.tmp`` → fsync → replace) before the record that references them is
saved, so a failed rewrite never destroys a previous output; JSONL appends write one whole line
per call and flush (not fsync) — readers tolerate a torn trailing line after a crash.

Permissions: the store holds inputs, transcripts and outputs, so every directory it
creates (``$RAYSPEC_HOME`` and ``projects/<slug>/`` included when they do not exist yet,
``runs/<id>/``, ``steps/<path>/``) is ``0700`` and every file it writes is ``0600``,
independent of the process umask (:func:`secure_mkdir` / :func:`open_private`). Pre-existing
directories are never re-chmodded. The other writers under ``$RAYSPEC_HOME`` that run before or
beside the store — the workdir lock (``projects/<slug>/locks/*.lock``, usually the first writer
on a run), the step ``context.json`` and the executor ``stdout.log``/``stderr.log`` — use the
same two helpers; ``worktrees/`` (git checkouts, registry) is the remaining umask-mode writer.

Redaction: the store carries a :class:`~rayspec.redact.Redactor` (``NULL_REDACTOR`` by
default; the CLI installs the real one once the run's secrets are resolved) and applies it to
**every** byte it writes — ``run.json``, output files, ``events.jsonl`` and ``stream.jsonl``.
Records and events are redacted as serialised JSON text, which is why the redactor also knows
each value's JSON-escaped form. Streamed text is redacted through a
:class:`~rayspec.redact.StreamRedactor` per ``(run, step, kind, attempt)`` so a secret split
across two deltas is still caught; only a tail that could still grow into a secret is held back,
and :meth:`FileRunStore.append_event` flushes it on ``step.finished`` and
``run.finished``/``run.paused`` (:meth:`FileRunStore.flush_streams` is the explicit form). What
``stream.jsonl`` reassembles to is therefore exactly what the step produced — redaction moves
chunk boundaries, it never drops text. That is the whole point of routing new writes through the
store: a writer that opens a file under the run dir directly is not covered.

The optional ``audit.jsonl`` (:func:`audit_log_enabled`) is one row per governance-relevant
fact — the run and who started it, its steps, the commands and tools its agents used, the files
they changed, the approvals and who made them. It is derived from what already flows through
:meth:`FileRunStore.create`, :meth:`~FileRunStore.append_event` and
:meth:`~FileRunStore.append_stream`, and redacted over its VALUES rather than its serialised
text. It is a **log**: append-only in behaviour, with nothing about the file proving it was not
edited afterwards, and strictly local to one run.
"""

from __future__ import annotations

import errno
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import stat
import threading
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, TextIO, TypeVar, cast

from pydantic import ValidationError

from rayspec.engine.paths import StepPath
from rayspec.errors import RayspecError
from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.redact import NULL_REDACTOR, Redactor, StreamRedactor
from rayspec.store.model import RunRecord, StepRecord

_log = logging.getLogger(__name__)
T = TypeVar("T")

#: Modes of everything the store creates (independent of the umask).
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
#: ``os.open`` flags that are not available on every platform (``0`` when missing).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CHUNK_CHARS = 1 << 20  # characters per write while streaming large outputs
_OUTPUT_FILES = {"text": "output.txt", "json": "output.json"}

RUN_JSON = "run.json"
#: Where a step's declared ``artifacts:`` are copied (``artifacts/<step path>/<declared path>``).
ARTIFACTS_DIR = "artifacts"
#: Bytes per read while copying an artifact (constant memory for a large file).
_ARTIFACT_CHUNK_BYTES = 1 << 20
#: The rendered prompt of a ``prompt:`` step (``steps/<path>/prompt.txt``).
PROMPT_TXT = "prompt.txt"
EVENTS_JSONL = "events.jsonl"
STREAM_JSONL = "stream.jsonl"
#: The optional local ledger (see :func:`audit_log_enabled`).
AUDIT_JSONL = "audit.jsonl"
#: Environment variable that turns the ledger on for every run of this process.
AUDIT_ENV = "RAYSPEC_AUDIT_LOG"
#: Longest ``detail`` an audit row keeps (a tool argument can be a whole file).
AUDIT_DETAIL_CAP = 1000
#: Values of :data:`AUDIT_ENV` that mean "off".
_AUDIT_FALSY = frozenset({"", "0", "false", "no", "off"})
_RUN_SUBDIRS = ("steps", "artifacts", "tmp")

#: Lifecycle events that become an audit row, and the row kind each one gets.
_AUDIT_EVENT_KINDS: dict[EventType, str] = {
    EventType.RUN_STARTED: "run",
    EventType.RUN_RESUMED: "run",
    EventType.RUN_PAUSED: "run",
    EventType.RUN_FINISHED: "run",
    EventType.WORKSPACE_CREATED: "run",
    EventType.RUN_DECISION: "approval",
    EventType.STEP_STARTED: "step",
    EventType.STEP_RETRY: "step",
    EventType.STEP_FINISHED: "step",
    EventType.WARNING: "warning",
}

#: Stream record kinds that become an audit row, and the row kind each one gets. Deltas,
#: transcripts and usage reports are NOT here: the ledger answers "what did it DO", and the
#: full text is one file away in ``stream.jsonl``.
_AUDIT_STREAM_KINDS: dict[str, str] = {
    "command_start": "command",
    "tool_call": "tool",
    "file_change": "file",
    "warning": "warning",
    "error": "warning",
}


def audit_log_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether :data:`AUDIT_ENV` asks for a local ``audit.jsonl`` next to the run.

    Off by default: the ledger is a convenience for somebody who wants one file per run they can
    read or keep, not a second copy of the transcript everybody pays for.
    """
    env = os.environ if env is None else env
    value = env.get(AUDIT_ENV)
    return value is not None and value.strip().lower() not in _AUDIT_FALSY


def _raw_detail(text: Any) -> str:
    """A row's ``detail`` exactly as it came off the event or the record.

    Deliberately unshortened: the redactor matches a secret **literally**, so collapsing the
    whitespace out of a PEM key or cutting a long token in half here would leave a value the
    redactor can no longer recognise. Shaping is :func:`finish_audit_row`'s job, afterwards.
    """
    return str(text or "")


def _shape_detail(text: Any) -> str:
    """One capped line of plain text for an audit row's ``detail`` — redacted text only."""
    line = " ".join(str(text or "").split())
    if len(line) > AUDIT_DETAIL_CAP:
        line = line[: AUDIT_DETAIL_CAP - 1] + "…"
    return line


def finish_audit_row(row: dict[str, Any], redactor: Redactor = NULL_REDACTOR) -> dict[str, Any]:
    """A raw ledger row, **redacted first and shaped second** — never the other way round.

    :func:`audit_entry_for_event` and :func:`audit_entry_for_stream` return the row with its
    text untouched; this is what turns it into the row that is written or printed. The order is
    the whole point: redaction is exact match, so a multi-line secret survives a whitespace
    collapse and an overlong one survives a truncation, and either would then be persisted.

    ``redactor`` defaults to the no-op, which is right for a caller re-deriving rows from files
    the store already redacted on the way in (``rayspec audit``).
    """
    row = dict(redactor.redact_obj(row)) if redactor else dict(row)
    data = row.get("data")
    if isinstance(data, Mapping):
        shaped = dict(data)
        if isinstance(shaped.get("input"), str):
            shaped["input"] = _shape_detail(shaped["input"])
        row["data"] = shaped
    row["detail"] = _shape_detail(row.get("detail"))
    return row


def audit_entry_for_event(event: RunEvent) -> dict[str, Any] | None:
    """The ledger row for one lifecycle event, or ``None`` when it carries no governance fact.

    Rows are ``{ts, kind, step, detail, data}``: ``kind`` is one of ``run``/``step``/
    ``approval``/``warning``, ``detail`` is the one-line summary and ``data`` the event's own
    payload. Progress events (loop iterations, ``each`` items) are deliberately dropped — they
    say how far a run got, not what it did.

    The row is **raw**: pass it through :func:`finish_audit_row` before writing or printing it.
    """
    kind = _AUDIT_EVENT_KINDS.get(event.type)
    if kind is None:
        return None
    data = dict(event.data)
    if event.type is EventType.RUN_DECISION:
        detail = "approved" if data.get("approved") else "rejected"
    elif event.type is EventType.WARNING:
        detail = _raw_detail(data.get("message") or data.get("warning"))
    elif event.type is EventType.STEP_FINISHED:
        detail = _raw_detail(data.get("status") or "finished")
    elif event.type is EventType.STEP_STARTED:
        detail = _raw_detail(f"started ({data.get('kind') or 'step'})")
    elif event.type is EventType.STEP_RETRY:
        detail = _raw_detail(f"retry {data.get('attempt')}")
    elif event.type is EventType.RUN_FINISHED:
        detail = _raw_detail(f"finished ({data.get('status') or 'unknown'})")
    elif event.type is EventType.WORKSPACE_CREATED:
        detail = _raw_detail(data.get("branch") or data.get("workdir"))
    else:
        detail = event.type.value.split(".", 1)[1]
    return {
        "ts": event.ts.isoformat(),
        "kind": kind,
        "step": event.step_path,
        "detail": detail,
        "data": data,
    }


def audit_entry_for_stream(step_path: str, record: StreamRecord) -> dict[str, Any] | None:
    """The ledger row for one stream record, or ``None`` for the kinds the ledger ignores.

    A ``command_start`` becomes a ``command`` row (the command line), a ``tool_call`` a ``tool``
    row (the tool name, with its arguments in ``data``), a ``file_change`` a ``file`` row (the
    path) and a ``warning``/``error`` a ``warning`` row.

    The row is **raw**: pass it through :func:`finish_audit_row` before writing or printing it.
    """
    kind = _AUDIT_STREAM_KINDS.get(record.kind)
    if kind is None:
        return None
    if record.kind == "command_start":
        detail = _raw_detail(record.data.get("command") or record.text)
        data: dict[str, Any] = {"attempt": record.attempt}
    elif record.kind == "tool_call":
        detail = _raw_detail(record.name or "tool")
        arguments = record.data.get("input")
        data = {"attempt": record.attempt, "call_id": record.call_id}
        if arguments is not None:
            data["input"] = json.dumps(arguments, ensure_ascii=False, default=str)
    elif record.kind == "file_change":
        first = record.text.strip().splitlines()
        detail = _raw_detail(record.name or (first[0] if first else ""))
        data = {"attempt": record.attempt}
    else:
        detail = _raw_detail(record.text)
        data = {"attempt": record.attempt, "level": record.kind}
    return {
        "ts": record.ts.isoformat(),
        "kind": kind,
        "step": step_path,
        "detail": detail,
        "data": data,
    }


class StoreError(RayspecError):
    """Base class for run-store errors (unknown/ambiguous run ids, duplicate runs)."""


class UnknownRunIdError(StoreError):
    """No run matches the given id or prefix."""


class AmbiguousRunIdError(StoreError):
    """A run-id prefix matches more than one run; ``candidates`` lists them (newest first)."""

    def __init__(self, prefix: str, candidates: list[str]):
        self.prefix = prefix
        self.candidates = candidates
        shown = ", ".join(candidates[:5]) + (" …" if len(candidates) > 5 else "")
        super().__init__(
            f"run id prefix {prefix!r} is ambiguous: {shown}",
            hint="use a longer prefix or the full run id",
        )


class RunExistsError(StoreError):
    """``create`` was called for a run id that already has a directory."""


class CorruptRunError(StoreError):
    """``run.json`` exists but is not a valid :class:`RunRecord` (bad JSON/shape/bytes)."""


@dataclass(frozen=True, slots=True)
class WrittenOutput:
    """Result of :meth:`FileRunStore.write_output_with_sha`.

    ``output_ref`` is run-dir relative (the value stored in ``StepRecord.output_ref``);
    ``sha256``/``size`` describe the bytes actually on disk (JSON outputs are hashed in their
    pretty-printed form).
    """

    output_ref: str
    path: Path
    kind: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class WrittenArtifact:
    """Result of :meth:`FileRunStore.write_artifact`.

    ``artifact_ref`` is run-dir relative (the value stored in ``StepRecord.artifacts[].ref``),
    ``path`` the copy inside the run directory, ``source`` where the step wrote the file, and
    ``sha256``/``size`` describe the bytes actually stored (which is what redaction may have
    changed, not what the step wrote).
    """

    artifact_ref: str
    path: Path
    source: Path
    sha256: str
    size: int


class FileRunStore:
    """Run store rooted at ``root`` (typically ``~/.rayspec/projects/<slug>``).

    Thread-safe for a single process: ``save`` is serialised by one lock (single writer), JSONL
    appends by another, so it is safe to call the sync methods from worker threads
    (``anyio.to_thread.run_sync``) or from several anyio tasks. The append lock is process-wide
    (all runs, all steps) and every append re-opens the file: trivially correct and cheap at v1
    volumes (a few deltas per second); per-path handles/locks are a later optimisation.
    """

    def __init__(
        self, root: Path, *, redactor: Redactor = NULL_REDACTOR, audit: bool | None = None
    ):
        self.root = Path(root)
        self.runs_root = self.root / "runs"
        self._save_lock = threading.Lock()
        self._append_lock = threading.Lock()
        self._tmp_counter = itertools.count()
        #: applied to every byte this store writes. The store is built before the run's
        #: secrets are known, so the CLI assigns the real redactor to this attribute at run
        #: start; ``NULL_REDACTOR`` (the default) is a no-op the writers skip entirely.
        self.redactor: Redactor = redactor
        #: ``True``/``False`` pin the local ledger on or off; ``None`` (the default) asks
        #: :data:`AUDIT_ENV` at write time, so a process that exports it mid-flight is honoured.
        self.audit: bool | None = audit
        self._stream_redactors: dict[tuple[str, str, str, int], StreamRedactor] = {}

    # -- paths --------------------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        """Directory of a run (not created). Raises ``ValueError`` for unsafe ids."""
        return self.runs_root / _check_run_id(run_id)

    def step_dir(self, run_id: str, step_path: str) -> Path:
        """``steps/<path>`` for a run, created (with parents, ``0700``) on first use.

        Raises ``ValueError`` for an invalid or empty step path (the root path is never a step).
        """
        path = self._step_dir_path(run_id, step_path)
        secure_mkdir(path)
        return path

    def _step_dir_path(self, run_id: str, step_path: str) -> Path:
        parsed = StepPath.parse(step_path)
        if parsed.is_root:
            raise ValueError("step path must not be empty")
        return self.run_dir(run_id) / "steps" / parsed.fs_path()

    def exists(self, run_id: str) -> bool:
        """True if the run has a ``run.json``."""
        return (self.run_dir(run_id) / RUN_JSON).is_file()

    # -- run.json -----------------------------------------------------------------------------

    def create(self, run: RunRecord) -> None:
        """Create the run directory skeleton and write the first ``run.json``.

        With the local ledger enabled this is also its first row: the workflow, the project and
        — the fact no event carries — WHO started the run.
        """
        run_dir = self.run_dir(run.run_id)
        if run_dir.exists():
            raise RunExistsError(f"run {run.run_id!r} already exists at {run_dir}")
        self.save(run)
        self._append_audit(
            run.run_id,
            {
                "ts": run.created_at.isoformat(),
                "kind": "run",
                "step": None,
                "detail": "created",
                "data": {
                    "workflow": run.workflow_name,
                    "project_slug": run.project_slug,
                    "dry_run": run.dry_run,
                    "actor": None if run.actor is None else run.actor.model_dump(mode="json"),
                },
            },
        )

    def save(self, run: RunRecord) -> None:
        """Atomically persist the whole record: tmp file + fsync + ``os.replace``.

        The run directory skeleton (``steps/``, ``artifacts/``, ``tmp/``) is ensured as well, so
        a run saved without :meth:`create` is still a complete run dir.
        """
        run_dir = self.run_dir(run.run_id)
        _ensure_skeleton(run_dir)
        payload = self.redactor.redact(run.model_dump_json(indent=2) + "\n")
        with self._save_lock:
            tmp = run_dir / f"{RUN_JSON}.{os.getpid()}.{next(self._tmp_counter)}.tmp"
            try:
                with open_private(tmp, "w") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, run_dir / RUN_JSON)
            except BaseException:
                _unlink_quietly(tmp)
                raise
            _fsync_dir(run_dir)

    def load(self, run_id: str) -> RunRecord:
        """Load ``run.json``; unknown keys are ignored (forward compatible).

        Raises :class:`UnknownRunIdError` if there is no ``run.json`` and
        :class:`CorruptRunError` if it cannot be parsed into a :class:`RunRecord`.
        """
        path = self.run_dir(run_id) / RUN_JSON
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise UnknownRunIdError(f"unknown run {run_id!r}") from None
        except UnicodeDecodeError as exc:
            raise CorruptRunError(f"run.json of {run_id!r} at {path} is not UTF-8: {exc}") from exc
        try:
            return RunRecord.model_validate_json(text)
        except ValidationError as exc:
            raise CorruptRunError(
                f"run.json of {run_id!r} at {path} is not a valid RunRecord: {exc}"
            ) from exc

    # -- listing / resolution -----------------------------------------------------------------

    def list_run_ids(self) -> list[str]:
        """Run ids that have a ``run.json``, newest first (ids are time-sortable)."""
        if not self.runs_root.is_dir():
            return []
        ids = [
            entry.name
            for entry in os.scandir(self.runs_root)
            if entry.is_dir() and _RUN_ID_RE.match(entry.name) and self.exists(entry.name)
        ]
        return sorted(ids, reverse=True)

    def list_runs(self, *, limit: int | None = None) -> list[RunRecord]:
        """Load runs newest first; unreadable ``run.json`` files are skipped with a warning."""
        runs: list[RunRecord] = []
        for run_id in self.list_run_ids():
            if limit is not None and len(runs) >= limit:
                break
            try:
                runs.append(self.load(run_id))
            except Exception as exc:  # listing must survive one bad file
                _log.warning("skipping unreadable run %s: %s", run_id, exc)
        return runs

    def resolve_run_id(self, prefix: str) -> str:
        """Resolve a full id or unique prefix to a run id (exact match always wins)."""
        ids = self.list_run_ids()
        if prefix and prefix in ids:
            return prefix
        candidates = [rid for rid in ids if rid.startswith(prefix)] if prefix else []
        if not candidates:
            raise UnknownRunIdError(
                f"no run matches {prefix!r}", hint="run `rayspec runs` to list known runs"
            )
        if len(candidates) > 1:
            raise AmbiguousRunIdError(prefix, candidates)
        return candidates[0]

    def delete_run(self, run_id: str) -> None:
        """Remove a run directory (outputs, logs, artifacts included)."""
        run_dir = self.run_dir(run_id)
        if not run_dir.exists():
            raise UnknownRunIdError(f"unknown run {run_id!r}")
        shutil.rmtree(run_dir)

    # -- outputs ------------------------------------------------------------------------------

    def write_output(self, run_id: str, step_path: str, content: str, *, kind: str) -> str:
        """Write ``steps/<path>/output.txt|json`` (fsync) and return its run-dir-relative ref."""
        return self.write_output_with_sha(run_id, step_path, content, kind=kind).output_ref

    def write_output_with_sha(
        self, run_id: str, step_path: str, content: str, *, kind: str
    ) -> WrittenOutput:
        """Like :meth:`write_output` but also reports sha256/size of the written bytes.

        ``kind`` is ``"text"`` (written verbatim, streamed in chunks) or ``"json"`` (``content``
        must be a strict JSON document — ``NaN``/``Infinity`` are rejected; it is re-serialised
        pretty-printed with a trailing newline). A stale output file of the other kind is removed.
        The file is written as ``output.<kind>.<pid>.<n>.tmp`` + fsync + ``os.replace``: a failed
        rewrite leaves the previous output intact.

        Redaction happens before hashing, so the sha is the file's — and for ``json`` it
        happens on the PARSED value, not on the serialised text: a secret that is a bare JSON
        token (a number, ``true``, ``null``) would otherwise turn a valid document into an
        invalid one. The marker replaces the value as a *string*, and the document stays
        well-formed.
        """
        try:
            filename = _OUTPUT_FILES[kind]
        except KeyError:
            raise ValueError(f"unknown output kind {kind!r}; expected 'text' or 'json'") from None
        if kind == "json":
            try:
                parsed = json.loads(content, parse_constant=_reject_json_constant)
                parsed = self.redactor.redact_obj(parsed)  # the value, not the text
                content = json.dumps(parsed, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            except ValueError as exc:  # JSONDecodeError is a ValueError too
                raise ValueError(f"output of step {step_path!r} is not valid JSON: {exc}") from exc
        else:
            content = self.redactor.redact(content)
        step_dir = self.step_dir(run_id, step_path)
        for other_kind, other_name in _OUTPUT_FILES.items():
            if other_kind != kind:
                _unlink_quietly(step_dir / other_name)
        path = step_dir / filename
        tmp = step_dir / f"{filename}.{os.getpid()}.{next(self._tmp_counter)}.tmp"
        sha, size = _write_text_durably(path, tmp, _chunks(content))
        rel = path.relative_to(self.run_dir(run_id)).as_posix()
        return WrittenOutput(output_ref=rel, path=path, kind=kind, sha256=sha, size=size)

    def write_prompt(self, run_id: str, step_path: str, text: str) -> str:
        """Write ``steps/<path>/prompt.txt`` (fsync) and return its run-dir-relative ref.

        Additive sibling of :meth:`write_output` for the *input* side of a ``prompt:`` step: the
        fully rendered prompt the provider was handed, written **before** the call, so
        ``rayspec explain --full`` can show the exact bytes even when the attempt never produced
        an output. Same durability and permissions as an output (``prompt.txt.<pid>.<n>.tmp`` +
        fsync + ``os.replace``, ``0600`` through :func:`open_private`); a later attempt of the
        same step overwrites it. Read it back with :meth:`read_output` (the ref is a plain
        run-dir-relative path).
        """
        step_dir = self.step_dir(run_id, step_path)
        path = step_dir / PROMPT_TXT
        tmp = step_dir / f"{PROMPT_TXT}.{os.getpid()}.{next(self._tmp_counter)}.tmp"
        _write_text_durably(path, tmp, _chunks(text))
        return path.relative_to(self.run_dir(run_id)).as_posix()

    def write_artifact(
        self, run_id: str, step_path: str, rel_path: str, source: Path
    ) -> WrittenArtifact:
        """Copy one declared artifact into ``artifacts/<step path>/<rel_path>`` and describe it.

        Additive sibling of :meth:`write_output` for the files a step promises under
        ``artifacts:``: the run directory keeps its own copy, so a run stays self-describing
        after the worktree is gone. Same durability and permissions as an output (tmp file +
        fsync + ``os.replace``, ``0600`` through :func:`open_private`, ``0700`` directories);
        a later attempt of the same step overwrites the copy.

        ``rel_path`` must be run-dir safe (relative, no ``..``) — the schema already refuses
        anything else at load time, and this is the second lock on the door: a ``ValueError``
        here means a caller bypassed it.

        Redaction applies to artifacts like to every other byte the store writes, and works on
        arbitrary bytes: the file is decoded with ``surrogateescape``, redacted and encoded back,
        which round-trips a binary file exactly while still catching a secret that a step wrote
        into one. A file that *did* contain a secret is stored with the marker in its place, so
        its bytes (and its sha) differ from the step's original — that is the point.
        """
        rel = PurePosixPath(rel_path)
        if not rel_path or rel.is_absolute() or ".." in rel.parts or rel.name in {"", ".", ".."}:
            raise ValueError(f"artifact path must be relative to the workdir: {rel_path!r}")
        run_dir = self.run_dir(run_id)
        parsed = StepPath.parse(step_path)
        if parsed.is_root:
            raise ValueError("step path must not be empty")
        path = run_dir / ARTIFACTS_DIR / parsed.fs_path() / Path(*rel.parts)
        if not path.resolve(strict=False).is_relative_to(run_dir.resolve(strict=False)):
            raise ValueError(f"artifact path {rel_path!r} escapes the run directory")
        secure_mkdir(path.parent)
        tmp = path.parent / f"{path.name}.{os.getpid()}.{next(self._tmp_counter)}.tmp"
        sha, size = _copy_bytes_durably(Path(source), path, tmp, self.redactor)
        return WrittenArtifact(
            artifact_ref=path.relative_to(run_dir).as_posix(),
            path=path,
            source=Path(source),
            sha256=sha,
            size=size,
        )

    def read_output(self, run_id: str, output_ref: str) -> str:
        """Read an output by its run-dir-relative ref (``FileNotFoundError`` if missing).

        ``ValueError`` if the ref is absolute, contains ``..`` or resolves (through symlinks)
        outside the run directory.
        """
        return self._resolve_ref(run_id, output_ref).read_text(encoding="utf-8")

    def record_step(
        self,
        run: RunRecord,
        record: StepRecord,
        output: str | None = None,
        *,
        kind: str = "text",
    ) -> WrittenOutput | None:
        """Write-ahead helper: output file → record → ``run.json``, in that order.

        When ``output`` is given it is written first and ``record.output_ref``/``output_kind``/
        ``output_sha256`` are filled in; then ``record`` is stored under ``run.steps[record.path]``
        and the run is saved. Returns the written output description (or ``None``).

        Both writes go through the redactor — the output file directly, the record as
        part of ``run.json`` — and the step's held-back stream tail is flushed first, so a
        finished step's ``stream.jsonl`` is complete before ``run.json`` points at it. (The
        engine persists steps through :meth:`save`; the flush that covers it is the one
        :meth:`append_event` does on ``step.finished``.)
        """
        self.flush_streams(run.run_id, record.path)  # no held-back delta may be lost
        written: WrittenOutput | None = None
        if output is not None:
            written = self.write_output_with_sha(run.run_id, record.path, output, kind=kind)
            record.output_ref = written.output_ref
            record.output_kind = written.kind
            record.output_sha256 = written.sha256
        run.steps[record.path] = record
        self.save(run)
        return written

    # -- events / streams ---------------------------------------------------------------------

    def append_event(self, run_id: str, event: RunEvent) -> None:
        """Append one lifecycle event to ``events.jsonl`` (one line, flushed, redacted).

        ``step.finished``/``run.finished`` are also what END a stream, so this is where the
        boundary buffer of :meth:`append_stream` is flushed: the engine emits both for
        every step and every run — including a failed, cancelled or paused one — so no held-back
        tail can be lost, and nothing outside the store has to remember the rule.
        """
        if self._stream_redactors:
            if event.type is EventType.STEP_FINISHED and event.step_path:
                self.flush_streams(run_id, event.step_path)
            elif event.type is EventType.RUN_FINISHED or event.type is EventType.RUN_PAUSED:
                self.flush_streams(run_id)
        self._append_line(
            self.run_dir(run_id) / EVENTS_JSONL, self.redactor.redact(event.to_json())
        )
        self._append_audit(run_id, audit_entry_for_event(event))

    def _append_audit(self, run_id: str, entry: dict[str, Any] | None) -> None:
        """Append one ledger row to ``audit.jsonl`` — values redacted, never the serialised text.

        A no-op unless the ledger is enabled. :func:`finish_audit_row` redacts the row's VALUES
        (:meth:`~rayspec.redact.Redactor.redact_obj`), so a numeric secret becomes the marker
        instead of corrupting the JSON, and a complete string is matched in one piece — there is
        no chunk boundary here for a value to hide across — and only then shortens the ``detail``
        to one capped line.

        The ledger is a log, not evidence: rows are appended in the order the store learns them
        and nothing about the file proves it was not edited afterwards.
        """
        if entry is None or not (audit_log_enabled() if self.audit is None else self.audit):
            return
        payload = finish_audit_row(entry, self.redactor)
        self._append_line(
            self.run_dir(run_id) / AUDIT_JSONL,
            json.dumps(payload, ensure_ascii=False, default=str),
        )

    def read_audit(self, run_id: str) -> Iterator[dict[str, Any]]:
        """Iterate the rows of ``audit.jsonl`` (empty when the ledger was never enabled).

        A torn trailing line ends the iteration and an unreadable middle line is skipped, both
        with a warning — the same tolerance :meth:`read_events` and :meth:`read_stream` have.
        """
        path = self.run_dir(run_id) / AUDIT_JSONL
        for line, terminated in _iter_lines(path):
            try:
                row = json.loads(line)
            except ValueError as exc:
                if not terminated:
                    _log.warning("%s: ignoring torn trailing line (%s)", path, exc)
                    return
                _log.warning("%s: skipping unreadable line (%s)", path, exc)
                continue
            if isinstance(row, dict):
                yield row

    def append_stream(self, run_id: str, step_path: str, record: StreamRecord) -> None:
        """Append one stream record to ``steps/<path>/stream.jsonl`` (one line, flushed).

        With a redactor installed the record's ``text`` goes through a
        :class:`~rayspec.redact.StreamRedactor` for its ``(run, step, kind, attempt)`` so a
        secret split across two deltas is caught; the rest of the line is redacted as JSON text.
        Only a tail that could still grow into a secret is held back, and it is written by
        :meth:`flush_streams` — which :meth:`append_event` calls on ``step.finished`` and
        ``run.finished``/``run.paused``, so a finished stream is always complete on disk.
        """
        # derived from the ORIGINAL record: a boundary buffer may hold back the tail of the
        # very command the ledger is about, and a whole value redacts more reliably than a chunk
        self._append_audit(run_id, audit_entry_for_stream(step_path, record))
        if self.redactor:
            record = self._redact_stream(run_id, step_path, record)
        self._append_line(
            self.step_dir(run_id, step_path) / STREAM_JSONL, self.redactor.redact(record.to_json())
        )

    def flush_streams(self, run_id: str, step_path: str | None = None) -> None:
        """Write the text a :meth:`append_stream` boundary buffer is still holding back.

        ``step_path`` limits the flush to one step (what :meth:`record_step` does); without it
        every buffered stream of the run is flushed. A no-op without a redactor.
        """
        for key in [k for k in self._stream_redactors if k[0] == run_id]:
            if step_path is not None and key[1] != step_path:
                continue
            stream = self._stream_redactors.pop(key)
            tail = stream.flush()
            if tail:
                record = StreamRecord(kind=key[2], attempt=key[3], text=tail)
                self._append_line(
                    self.step_dir(run_id, key[1]) / STREAM_JSONL,
                    self.redactor.redact(record.to_json()),
                )

    def _redact_stream(self, run_id: str, step_path: str, record: StreamRecord) -> StreamRecord:
        """``record`` with its ``text`` passed through the step's boundary-safe buffer."""
        if not record.text:
            return record
        key = (run_id, step_path, record.kind, record.attempt)
        stream = self._stream_redactors.get(key)
        if stream is None:
            stream = self._stream_redactors[key] = StreamRedactor(self.redactor)
        return record.model_copy(update={"text": stream.feed(record.text)})

    def read_events(self, run_id: str) -> Iterator[RunEvent]:
        """Iterate ``events.jsonl`` (empty if absent).

        A torn trailing line (crash/SIGINT mid-write) ends the iteration with a warning; an
        unparseable line in the middle is skipped with a warning.
        """
        yield from _iter_records(self.run_dir(run_id) / EVENTS_JSONL, RunEvent.from_json)

    def read_stream(
        self, run_id: str, step_path: str, *, kinds: Collection[str] | None = None
    ) -> Iterator[StreamRecord]:
        """Iterate ``steps/<path>/stream.jsonl`` (empty if absent); torn lines as in read_events.

        ``kinds`` restricts the result to records of those kinds and makes
        the scan cheap: a line is parsed only when it contains one of the kind names as a JSON
        string (``"warning"``), so ``rayspec show`` finds the warnings of a multi-MB transcript
        without validating every delta.
        """
        path = self._step_dir_path(run_id, step_path) / STREAM_JSONL
        if kinds is None:
            yield from _iter_records(path, StreamRecord.from_json)
            return
        wanted = frozenset(kinds)
        needles = [f'"{kind}"' for kind in wanted]
        for record in _iter_records(
            path, StreamRecord.from_json, prefilter=lambda line: any(n in line for n in needles)
        ):
            if record.kind in wanted:
                yield record

    # -- internals ----------------------------------------------------------------------------

    def _append_line(self, path: Path, line: str) -> None:
        secure_mkdir(path.parent)
        with self._append_lock, open_private(path, "a") as fh:
            fh.write(line + "\n")
            fh.flush()

    def _resolve_ref(self, run_id: str, output_ref: str) -> Path:
        run_dir = self.run_dir(run_id)
        rel = Path(output_ref)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"output_ref must be run-dir relative: {output_ref!r}")
        path = run_dir / rel
        if not path.resolve(strict=False).is_relative_to(run_dir.resolve(strict=False)):
            raise ValueError(f"output_ref {output_ref!r} escapes the run directory")
        return path


def _check_run_id(run_id: str) -> str:
    if not _RUN_ID_RE.match(run_id):  # the regex also excludes "." and ".."
        raise ValueError(f"invalid run id {run_id!r}")
    return run_id


def _chunks(text: str) -> Iterator[str]:
    for start in range(0, len(text), _CHUNK_CHARS):
        yield text[start : start + _CHUNK_CHARS]


def _reject_json_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def _ensure_skeleton(run_dir: Path) -> None:
    for sub in _RUN_SUBDIRS:
        secure_mkdir(run_dir / sub)


def secure_mkdir(path: Path) -> None:
    """``mkdir -p`` with mode ``0700`` for every directory that does not exist yet.

    Directories that already exist — a user's ``~/.rayspec`` created by hand, ``projects/`` from
    an older version — are left exactly as they are; only the ones created by this call are
    chmodded (``mkdir(mode=)`` alone is subject to the umask).
    """
    path = Path(path)
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=PRIVATE_DIR_MODE)
        except FileExistsError:
            continue  # created concurrently (another run of the same project)
        try:
            os.chmod(directory, PRIVATE_DIR_MODE)
        except OSError as exc:  # pragma: no cover - e.g. a filesystem without modes
            _log.debug("could not chmod %s: %s", directory, exc)


def open_private(
    path: Path, mode: str = "w", *, encoding: str = "utf-8", newline: str | None = None
) -> TextIO:
    """``open(path, mode)`` that creates the file ``0600`` (``w``/``a``/``x`` modes, text).

    The mode is applied at creation through ``os.open`` (umask can only remove bits from
    ``0600``), so a new file is never readable by others even for an instant; an existing file
    keeps its mode. A symlink at ``path`` is refused (``O_NOFOLLOW``, raises ``OSError``) so a
    planted link cannot redirect the write, and the descriptor is close-on-exec.
    """
    if mode not in {"w", "a", "x"}:
        raise ValueError(f"open_private supports 'w', 'a' or 'x', not {mode!r}")
    flags = os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC
    if mode == "w":
        flags |= os.O_TRUNC
    elif mode == "a":
        flags |= os.O_APPEND
    else:
        flags |= os.O_EXCL
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        return cast(TextIO, os.fdopen(fd, mode, encoding=encoding, newline=newline))
    except BaseException:  # pragma: no cover - fdopen failing is exotic
        os.close(fd)
        raise


def _write_text_durably(path: Path, tmp: Path, chunks: Iterable[str]) -> tuple[str, int]:
    """Stream ``chunks`` (utf-8) to ``tmp``, fsync, ``os.replace`` onto ``path``.

    Returns ``(sha256, size_bytes)`` of the written bytes. On any failure ``tmp`` is removed and
    ``path`` (the previous output, if any) is left untouched.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with open_private(tmp, "w", newline="") as fh:
            for chunk in chunks:
                data = chunk.encode("utf-8")
                digest.update(data)
                size += len(data)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    _fsync_dir(path.parent)
    return digest.hexdigest(), size


def _open_private_bytes(path: Path) -> BinaryIO:
    """``open(path, "wb")`` that creates the file ``0600`` — the binary twin of
    :func:`open_private` (same ``O_NOFOLLOW``/``O_CLOEXEC`` guarantees)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW | _O_CLOEXEC
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        return cast(BinaryIO, os.fdopen(fd, "wb"))
    except BaseException:  # pragma: no cover - fdopen failing is exotic
        os.close(fd)
        raise


def _open_regular_file(source: Path) -> BinaryIO:
    """``open(source, "rb")`` that refuses anything but a regular file.

    Opened non-blocking and checked by ``fstat`` on the OPEN descriptor, so a FIFO, a socket or
    a device node raises instead of blocking the worker thread forever (a blocked thread cannot
    be cancelled, so the run would never end). The engine already refuses those before it gets
    here; this is the second lock on the door, for a caller that did not.
    """
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | _O_CLOEXEC)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise OSError(errno.EINVAL, f"not a regular file: {source}")
        return cast(BinaryIO, os.fdopen(fd, "rb"))
    except BaseException:
        os.close(fd)
        raise


def _copy_bytes_durably(source: Path, path: Path, tmp: Path, redactor: Redactor) -> tuple[str, int]:
    """Copy ``source`` to ``tmp`` (redacted, chunked), fsync, ``os.replace`` onto ``path``.

    Returns ``(sha256, size_bytes)`` of the bytes actually written. The redaction is
    boundary-safe (a :class:`~rayspec.redact.StreamRedactor` per copy) and byte-preserving for
    content that is not text: each chunk is decoded with ``surrogateescape`` and encoded back,
    so any byte sequence round-trips unless it contained a secret. On any failure ``tmp`` is
    removed and ``path`` (a previous copy, if any) is left untouched.
    """
    digest = hashlib.sha256()
    size = 0
    stream = redactor.stream() if redactor else None
    try:
        with _open_private_bytes(tmp) as out, _open_regular_file(source) as src:

            def write(text: str) -> None:
                nonlocal size
                data = text.encode("utf-8", "surrogateescape")
                digest.update(data)
                size += len(data)
                out.write(data)

            while chunk := src.read(_ARTIFACT_CHUNK_BYTES):
                text = chunk.decode("utf-8", "surrogateescape")
                write(stream.feed(text) if stream is not None else text)
            if stream is not None:
                write(stream.flush())
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    _fsync_dir(path.parent)
    return digest.hexdigest(), size


def _iter_lines(path: Path) -> Iterator[tuple[str, bool]]:
    """Yield ``(line, terminated)`` for non-empty lines; ``terminated`` is False for a torn tail."""
    if not path.is_file():
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            terminated = raw.endswith("\n")
            line = raw.rstrip("\n")
            if line:
                yield line, terminated


def _iter_records(
    path: Path, parse: Callable[[str], T], *, prefilter: Callable[[str], bool] | None = None
) -> Iterator[T]:
    """Parse JSONL records; a torn trailing line stops iteration, a bad middle line is skipped.

    ``prefilter`` (a cheap substring test) skips lines without parsing them — a torn or
    unreadable line that fails the filter is skipped silently like any other filtered line.
    """
    for line, terminated in _iter_lines(path):
        if prefilter is not None and not prefilter(line):
            continue
        try:
            yield parse(line)
        except (ValidationError, ValueError) as exc:
            if not terminated:
                _log.warning("%s: ignoring torn trailing line (%s)", path, exc)
                return
            _log.warning("%s: skipping unreadable line (%s)", path, exc)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - best effort
        _log.debug("could not remove %s: %s", path, exc)


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so the rename itself is durable (POSIX only)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - e.g. Windows
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(fd)


__all__ = [
    "AUDIT_DETAIL_CAP",
    "AUDIT_ENV",
    "AUDIT_JSONL",
    "EVENTS_JSONL",
    "PRIVATE_DIR_MODE",
    "PRIVATE_FILE_MODE",
    "PROMPT_TXT",
    "RUN_JSON",
    "STREAM_JSONL",
    "AmbiguousRunIdError",
    "CorruptRunError",
    "FileRunStore",
    "RunExistsError",
    "StoreError",
    "UnknownRunIdError",
    "WrittenOutput",
    "audit_entry_for_event",
    "audit_entry_for_stream",
    "audit_log_enabled",
    "finish_audit_row",
    "open_private",
    "secure_mkdir",
]
