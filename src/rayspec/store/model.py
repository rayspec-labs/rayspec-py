# SPDX-License-Identifier: Apache-2.0
"""Run/step records — the on-disk checkpoint shape (``run.json``)."""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from rayspec.providers.base import Usage
from rayspec.schema.common import RunStatus, StepStatus

RUN_RECORD_SCHEMA_VERSION = 1
_BASE32 = "abcdefghijklmnopqrstuvwxyz234567"


def new_run_id() -> str:
    """Time-sortable id: ``YYYYMMDD-HHMMSS-<4 base32 chars>``. CLI accepts unique prefixes."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = "".join(secrets.choice(_BASE32) for _ in range(4))
    return f"{stamp}-{suffix}"


def utcnow() -> datetime:
    """Now, as an aware UTC datetime — the one clock every record timestamp is stamped from."""
    return datetime.now(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )

    #: Fields of THIS record that are its address rather than its content — the strings
    #: something later resolves the record by. Redaction leaves them alone wherever the record
    #: turns up (:data:`rayspec.redact.IDENTITY_FIELDS_ATTR`): a ``secret: true`` value that
    #: happens to equal one of them is a collision, and rewriting it does not leak less, it
    #: loses the record. Each of these is derived from the project on disk — a workflow file,
    #: a step id, a directory — never from an input, so keeping it discloses nothing that
    #: reading the project would not. Everything else a record holds stays redacted.
    redaction_identity: ClassVar[tuple[str, ...]] = ()


class SessionRef(_Model):
    provider: str
    id: str


class ErrorInfo(_Model):
    type: str
    message: str
    transient: bool = False


class LoopInfo(_Model):
    iterations: int = 0
    converged: bool | None = None


class EachInfo(_Model):
    total: int = 0
    succeeded: int = 0
    failed: int = 0


class DenialInfo(_Model):
    """One tool call a provider refused during a ``prompt:`` step.

    Mirrors :class:`rayspec.providers.base.Denial`. Only what was refused is recorded — the
    tool's name, the provider's wording and the call id — never the arguments it was called
    with, which are step content and may quote anything the step had in hand.
    """

    tool: str
    reason: str = ""
    call_id: str | None = None


class ArtifactRef(_Model):
    """One file a step promised (``artifacts:``) and delivered.

    ``path`` is the path as declared (in normal form), relative to the step's working directory;
    ``ref`` is the run-dir-relative location of the copy the store kept (``artifacts/<step
    path>/<path>``, ``None`` for a store that keeps no copies). ``sha256``/``size`` describe the
    stored bytes — the file's content AFTER redaction, which differs from what the step wrote
    only where a secret was found; a store that keeps no copy reports the same digest, over the
    bytes it would have kept. Only the path is recorded — the content of an artifact never
    enters a record or an event.
    """

    path: str
    ref: str | None = None
    sha256: str
    size: int = 0


class ActorInfo(_Model):
    """Who acted — an identity for the ledger, never a credential and never a permission.

    ``id`` is the resolved identity and ``source`` says where it came from (``env`` for
    ``RAYSPEC_ACTOR``, ``os`` for the operating-system user, ``unknown`` when nothing answered).
    Every field here is resolved from sources the audited run cannot write: the environment as
    the *operator* set it and the operating system. No git configuration is read, in any scope,
    and no variable rayspec copied out of a ``.env`` file is read either — a ``shell:`` step can
    write both. ``ci`` names the CI system the process ran under when there is one, and
    ``provider_accounts`` maps a provider id to the account the environment NAMED — an account,
    never the key that authenticates it. :func:`rayspec.actor.resolve_actor` fills this in.
    """

    id: str
    source: str = "unknown"
    ci: str | None = None
    provider_accounts: dict[str, str] = Field(default_factory=dict)
    #: additive: a ``RAYSPEC_ACTOR`` that a ``.env`` file supplied. It is NOT ``id``: a run can
    #: write ``$RAYSPEC_HOME/.env`` and its checkout's ``.rayspec/.env``, so this is a claim
    #: made ON this machine BY a file, recorded so the refusal is visible rather than silent.
    #: ``None`` whenever no ``.env`` supplied one, and in records written before the field.
    declared_id: str | None = None


class StepRecord(_Model):
    #: ``path`` is the key this record is already stored under in ``RunRecord.steps`` and the
    #: directory its files live in, ``id`` is that path's last segment, and the two ``_ref``
    #: fields are run-dir-relative paths the store built out of ``path``. Redacting any of them
    #: hides nothing the mapping's own keys do not show, while ``rayspec explain``/``logs`` then
    #: fail to parse the step they were handed (``invalid step path '[REDACTED:…]'``) and the
    #: refs point at files that are not there.
    redaction_identity: ClassVar[tuple[str, ...]] = ("path", "id", "output_ref", "prompt_ref")

    path: str
    id: str
    kind: str
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    ok: bool | None = None
    exit_code: int | None = None
    approved: bool | None = None
    output_ref: str | None = None
    output_kind: str | None = None  # text | json | null
    output_sha256: str | None = None
    #: additive: run-dir-relative ref of the rendered prompt a ``prompt:`` step handed to
    #: the provider (``steps/<path>/prompt.txt``, written before the call by
    #: ``FileRunStore.write_prompt``); ``None`` for every other kind and for older records
    prompt_ref: str | None = None
    session_ref: SessionRef | None = None
    provider: str | None = None
    model: str | None = None
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None
    cost_source: str = "none"
    #: additive: at least one attempt of this step was interrupted / timed out before the
    #: provider reported any usage — ``usage`` is a lower bound, never "zero tokens spent"
    usage_unknown: bool = False
    #: additive: the files this step declared under ``artifacts:`` and wrote — path, the
    #: run-dir-relative copy the store kept, sha256 and size. Empty for a step that declared
    #: none (and for records written before the field existed); a step that declared one and did
    #: not write it never gets here, it fails.
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    #: additive: the tool calls the provider refused in this step (permission or sandbox).
    #: Empty for every non-prompt kind, for a step that was allowed everything, and for records
    #: written before the field existed. With the agent's ``on_denial: fail`` a non-empty list
    #: also fails the step; the default ``warn`` records them and lets the step stand.
    denials: list[DenialInfo] = Field(default_factory=list)
    error: ErrorInfo | None = None
    skip_reason: str | None = None
    tolerated: bool = False
    iteration: int | None = None
    item_index: int | None = None
    item_sha256: str | None = None
    loop: LoopInfo | None = None
    each: EachInfo | None = None
    fingerprint: str | None = None

    @property
    def reusable(self) -> bool:
        """Resume may replay this record instead of re-running the step.

        Callers must additionally check that the output file exists and that the step is not
        ``always_run``. Every succeeded step MUST write an output file (approve → ``''``,
        composites → JSON) or resume will re-run it.
        """
        if self.output_ref is None:
            return False
        if self.status is StepStatus.SUCCEEDED:
            return True
        return self.status is StepStatus.FAILED and self.tolerated


class WorkspaceInfo(_Model):
    #: the directory a resumed run runs in — ``rayspec resume``/``approve`` rebuild the engine's
    #: workspace from it, so a rewritten one fails every remaining step with ``cwd does not
    #: exist`` and the second half of the run is lost
    redaction_identity: ClassVar[tuple[str, ...]] = ("workdir",)

    isolation: str = "none"
    workdir: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None


class Decision(_Model):
    approved: bool
    comment: str = ""
    by: str = "cli"
    decided_at: datetime = Field(default_factory=utcnow)
    #: additive: who decided (:func:`rayspec.actor.resolve_actor` at the moment the decision was
    #: recorded). ``by`` says through which door the decision came (``cli``, ``tty``, ``--yes``,
    #: ``dry-run``), ``actor`` says whose hand it was — the two differ whenever somebody other
    #: than the person who launched the run answers the gate. ``None`` in older records.
    actor: ActorInfo | None = None


class PauseInfo(_Model):
    token: str
    step: str
    message: str
    #: additive: why the run is waiting. ``approval`` (the default, and what every pause before
    #: this field meant) is an ``approve:`` gate; ``budget`` is an operational spending envelope
    #: or circuit breaker that stopped the run so a person can look at it.
    reason: str = "approval"
    requested_at: datetime = Field(default_factory=utcnow)
    decision: Decision | None = None


class RunRecord(_Model):
    #: ``run_id`` names the directory this record lives in; ``workflow_name``/``workflow_path``
    #: are what ``resume``/``approve``/``reject``/``explain`` re-load the workflow by; and
    #: ``project_root`` is the project every one of those commands re-scopes itself to.
    #: :data:`rayspec.store.file.RUN_IDENTITY_FIELDS` is this tuple, under the name the store
    #: hands to its writers.
    redaction_identity: ClassVar[tuple[str, ...]] = (
        "run_id",
        "workflow_name",
        "workflow_path",
        "project_root",
    )

    schema_version: int = Field(default=RUN_RECORD_SCHEMA_VERSION, alias="schema")
    run_id: str
    workflow_name: str
    workflow_path: str
    workflow_hash: str
    project_slug: str
    project_root: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = RunStatus.RUNNING
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    resume_count: int = 0
    pid: int | None = None
    host: str | None = None
    #: start time of the process behind ``pid`` as an opaque string — the exact output of
    #: ``ps -o lstart= -p <pid>`` (or the ``/proc/<pid>/stat`` starttime on Linux without ``ps``);
    #: additive so ``rayspec cancel`` can tell a reused pid from the run's own process.
    #: Recorded with ``pid`` at launch and on every resume, ``None`` in older records.
    pid_started_at: str | None = None
    #: additive: last time this run's process proved it was still alive — a periodic timer
    #: while any step is in flight, plus a stamp right before/after every provider call
    #: (:mod:`rayspec.engine.liveness`). ``rayspec runs``/``show`` read it alongside ``pid`` to
    #: tell a genuinely running process from one whose record was never corrected after a crash
    #: (PRD-07 R2/R4); ``None`` in records written before the field existed, and read that way —
    #: liveness then falls back to the pid check alone.
    heartbeat_at: datetime | None = None
    #: ``--dry-run`` (stub providers, shell/python skipped unless ``--exec-shell``); additive
    #: field so listings can tell a rehearsal from a real run
    dry_run: bool = False
    #: additive: ``--fail-fast`` — the failure policy this run was STARTED with, so that a
    #: resume continues it with the same blast radius instead of draining what the first half
    #: would have cancelled. The workflow's own ``defaults.on_step_failure`` is not recorded:
    #: it belongs to the workflow, and the workflow hash already refuses a resume of a changed
    #: one. Like the flag, it may only ever be TIGHTENED — a resume that passes ``--fail-fast``
    #: sets it, a resume that does not can never clear it. ``False`` in older records.
    fail_fast: bool = False
    workspace: WorkspaceInfo = Field(default_factory=WorkspaceInfo)
    pause: PauseInfo | None = None
    outputs: dict[str, Any] | None = None
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    #: run-level cost source (additive): ``provider`` (every step with tokens reported a
    #: provider cost), ``table`` (at least one pricing-table estimate, none unknown), ``partial``
    #: (at least one step has tokens but no cost at all — ``cost_usd`` is a lower bound) or
    #: ``none`` (no cost anywhere). Computed by the engine (``engine.context.cost_source_of``)
    #: on every final status; older ``run.json`` files read as ``none``.
    cost_source: str = "none"
    #: additive: absolute path of the ``--stubs`` file given at launch (``None`` when the run
    #: was not scripted); ``resume``/``approve``/``reject`` reuse it, ``--stubs`` overrides it
    stubs_path: str | None = None
    #: additive: whether ``--fail-fast`` was in force for this run. The failure policy is a
    #: blast-radius control, and the CLI flag is the operator's override of the workflow's own
    #: ``defaults.on_step_failure`` — it lives on the command line, so without recording it the
    #: second half of a run (``resume``/``approve``/``reject``) silently ran under a looser
    #: policy than the first. Written at launch and re-written on every resume entry, where the
    #: flag may still TIGHTEN it (it never loosens); ``False`` in records written before the
    #: field existed, which is the behaviour those runs had.
    fail_fast: bool = False
    #: additive: names of the inputs declared ``secret: true``; their values are never
    #: persisted (``inputs`` holds ``"<secret>"`` for the ones that were given) and must be
    #: supplied again on every resume entry
    secret_inputs: tuple[str, ...] = ()
    #: additive: what was in effect when the run started —
    #: ``{rayspec, python, platform, providers: {id: {sdk_version, cli_version, cli_path}},
    #: models: {agent key: literal model id}}``, captured once by
    #: :func:`rayspec.engine.toolchain.capture_toolchain` (best effort: an unreachable provider
    #: is recorded with an ``error`` entry). Only a FIRST start captures it, so it always
    #: describes the run's start: a resume never (re-)captures, and a record written before the
    #: field existed therefore keeps ``None`` rather than gaining a resume-time toolchain.
    toolchain: dict[str, Any] | None = None
    #: additive: who launched the run (:func:`rayspec.actor.resolve_actor` at its FIRST start —
    #: a resume never overwrites it, so the field keeps naming whoever set the run going).
    #: ``None`` in records written before the field existed.
    actor: ActorInfo | None = None

    def total_usage(self) -> Usage:
        total = Usage()
        for rec in self.steps.values():
            total = total + rec.usage
        return total

    def total_cost_usd(self) -> float | None:
        costs = [rec.cost_usd for rec in self.steps.values() if rec.cost_usd is not None]
        return sum(costs) if costs else None


def identity_strings(model: BaseModel) -> frozenset[str]:
    """The strings ``model`` is ADDRESSED by: its own, and those of the records it holds singly.

    The declarations are the ``redaction_identity`` class variables of :class:`_Model`
    (:data:`rayspec.redact.IDENTITY_FIELDS_ATTR`), so this answers for a record shape that grows
    a new address field the moment it declares one — nobody has to come back here and add it.

    What the answer is FOR: a ``secret: true`` value that equals one of these strings cannot be
    redacted, because the record has to keep the string — ``resume``/``explain``/``approve``
    resolve the run by it, and :meth:`rayspec.redact.Redactor.redact_dump` therefore writes it
    in clear. Handing them to :meth:`rayspec.redact.Redactor.build` is what makes every writer
    give such a value the SAME treatment, instead of the console and ``events.jsonl`` hiding
    what ``run.json``, ``show``, ``runs`` and ``audit`` print.

    A record held in a **collection** — the run's ``steps``, keyed by step path — is deliberately
    not walked. Its addresses are per-step rather than per-run and a run learns them as it goes,
    so they are not a stable property of the run's redactor: counting them would mean a secret
    that equals a step id is redacted for the first half of a run and not for the half after a
    resume. They also need no help, being structural on both sides already (``RunEvent`` carries
    ``step_path`` as a field of its own, never inside the free-form ``data`` a redactor walks),
    so nothing about them ever disagrees.
    """
    found: set[str] = set()
    _collect_identity(model, found)
    return frozenset(found)


def _collect_identity(model: BaseModel, found: set[str]) -> None:
    """Add ``model``'s declared address strings, then descend into its record-valued fields."""
    for name in getattr(type(model), "redaction_identity", ()):
        held = getattr(model, name, None)
        if isinstance(held, str) and held:
            found.add(held)
    for name in type(model).model_fields:
        value = getattr(model, name, None)
        if isinstance(value, BaseModel):
            _collect_identity(value, found)


__all__ = [
    "RUN_RECORD_SCHEMA_VERSION",
    "ActorInfo",
    "ArtifactRef",
    "Decision",
    "DenialInfo",
    "EachInfo",
    "ErrorInfo",
    "LoopInfo",
    "PauseInfo",
    "RunRecord",
    "SessionRef",
    "StepRecord",
    "WorkspaceInfo",
    "identity_strings",
    "new_run_id",
    "utcnow",
]
