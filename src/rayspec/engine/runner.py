# SPDX-License-Identifier: Apache-2.0
"""Run lifecycle: create/resume the :class:`RunRecord`, run the root graph under the signal-aware
runtime, render the workflow ``outputs:``, finalize status/exit code.

Module boundary: the entry point the CLI (and ``rayspec approve|reject|resume``) calls.
It wires the core modules into a :class:`~rayspec.engine.context.RunContext`, owns the
resume cache rules (reuse iff record reusable + output file present + not ``always_run``;
hash mismatch ⇒ refuse unless ``--force``; a run still ``running`` under a live pid — or
recorded on another host — ⇒ refuse unless ``--force``; a run of another workflow ⇒ always
refused; inputs fixed — except ``secret: true`` inputs, which are never persisted and must be
supplied again on every resume; attempts continue), the workdir path lock (``home=`` given
⇒ acquired before the record is touched, released on every final status incl. pause, retaken on
resume),
provider open/close per run, and the final ``run.finished`` event. The workspace itself (worktrees,
``--repo``) is a parallel scope: the CLI passes a :class:`Workspace` (default: in place).

It also closes the redaction boundary: before the first byte of a run is written the runner makes
the store's :class:`~rayspec.redact.Redactor` cover the run's own secrets, so an embedder cannot
get it wrong by omission (:meth:`Runner._install_redactor`).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from anyio import to_thread

from rayspec.actor import resolve_actor
from rayspec.engine.approval import ApprovalPrompt
from rayspec.engine.context import ExecScope, RunContext, RunOptions, StepOutcome
from rayspec.engine.errors import EngineError, ResumeError, RunPaused, RunStopped
from rayspec.engine.graph import FAILED_LIKE, StepGraph
from rayspec.engine.paths import StepPath
from rayspec.engine.runtime import (
    Runtime,
    configure_default_executor,
    exit_code_for,
    run_with_signals,
    unwrap_exception_group,
)
from rayspec.engine.scheduler import run_graph
from rayspec.engine.toolchain import capture_toolchain
from rayspec.events.base import EventSink
from rayspec.events.model import EventType
from rayspec.limits.envelope import (
    ENVELOPE_PAUSE_STEP,
    FAILURE_PAUSE_REASON,
    OPERATIONAL_PAUSE_REASONS,
)
from rayspec.loader import ResolvedWorkflow
from rayspec.loader.inputs import (
    SECRET_PLACEHOLDER,
    env_var_name,
    redact_inputs,
    secret_input_names,
    split_secret_inputs,
)
from rayspec.providers.base import Provider, Usage
from rayspec.redact import MIN_REDACTABLE_LEN, NULL_REDACTOR
from rayspec.schema import RunStatus
from rayspec.store.base import RunStore
from rayspec.store.model import (
    ActorInfo,
    PauseInfo,
    RunRecord,
    StepRecord,
    WorkspaceInfo,
    new_run_id,
    utcnow,
)
from rayspec.templating import Scope, StepView, TemplateEngine, TemplateRenderError


@dataclass(slots=True)
class Workspace:
    """Where the run executes. Built by the CLI (``rayspec.workspace`` when available)."""

    isolation: str = "none"
    workdir: Path = field(default_factory=Path.cwd)
    branch: str | None = None
    base_branch: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None

    @classmethod
    def in_place(cls, root: Path) -> Workspace:
        """Run directly in ``root`` (``isolation: none``)."""
        return cls(isolation="none", workdir=Path(root))

    def info(self) -> WorkspaceInfo:
        return WorkspaceInfo(
            isolation=self.isolation,
            workdir=str(self.workdir),
            branch=self.branch,
            base_branch=self.base_branch,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )


@dataclass(slots=True)
class RunResult:
    """Summary returned by :meth:`Runner.run`."""

    run_id: str
    status: RunStatus
    exit_code: int
    run_dir: Path
    workspace: Workspace
    outputs: dict[str, Any] | None = None
    reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None
    #: ``provider`` | ``table`` | ``partial`` | ``none`` (``context.cost_source_of``)
    cost_source: str = "none"
    steps: dict[str, StepRecord] = field(default_factory=dict)
    reused: list[str] = field(default_factory=list)
    pause: PauseInfo | None = None
    interrupted: bool = False
    record: RunRecord | None = None


def fallback_project_slug(root: Path) -> str:
    """``local/<dirname>-<sha1(abspath)[:8]>`` (the workspace scope computes git-based slugs)."""
    absolute = str(Path(root).resolve())
    digest = hashlib.sha1(absolute.encode("utf-8")).hexdigest()[:8]
    return f"local/{Path(absolute).name}-{digest}"


class Runner:
    """One run (or resume) of a resolved workflow."""

    def __init__(
        self,
        resolved: ResolvedWorkflow,
        *,
        inputs: Mapping[str, Any],
        store: RunStore,
        project_root: Path,
        project_slug: str | None = None,
        project_name: str | None = None,
        sinks: EventSink | None = None,
        workspace: Workspace | None = None,
        options: RunOptions | None = None,
        approval_prompt: ApprovalPrompt | None = None,
        engine: TemplateEngine | None = None,
        providers: Mapping[str, Provider] | None = None,
        env: Mapping[str, str] | None = None,
        run_id: str | None = None,
        resume_run_id: str | None = None,
        price_table: Any = None,
        handle_signals: bool = True,
        executors: Mapping[str, Any] | None = None,
        home: Path | None = None,
        envelope: Any = None,
    ) -> None:
        self.resolved = resolved
        #: the public inputs (secrets split off into :attr:`secret_inputs`); on a resume the
        #: public ones are ignored (``run.json`` is authoritative) and only secrets are read
        self.inputs, self.secret_inputs = split_secret_inputs(
            inputs, secret_input_names(resolved.workflow)
        )
        self.store = store
        self.project_root = Path(project_root)
        self.project_slug = project_slug or fallback_project_slug(self.project_root)
        self.project_name = project_name or self.project_root.name
        self.sinks = sinks if sinks is not None else _null_sink()
        self.workspace = workspace or Workspace.in_place(self.project_root)
        self.options = options or RunOptions()
        self.approval_prompt = approval_prompt
        self.engine = engine or TemplateEngine()
        self.providers = providers
        self.env = env
        self.run_id = run_id
        self.resume_run_id = resume_run_id
        self.price_table = price_table
        self.handle_signals = handle_signals
        #: executor overrides (kind → coroutine fn); tests inject fakes here
        self.executors: dict[str, Any] = dict(executors or {})
        #: rayspec home; when given (and ``rayspec.workspace`` is importable) the run holds the
        #: workdir path lock ``<home>/projects/<slug>/locks/<sha1(workdir)>.lock`` while it runs
        self.home = Path(home) if home is not None else None
        #: the operator's cross-run spending envelope / circuit breaker
        #: (:class:`rayspec.limits.envelope.RunEnvelope`); ``None`` = no policy caps this machine
        self.envelope = envelope
        self.ctx: RunContext | None = None
        self._lock: Any = None
        #: names this run declared secret whose value is too short to redact, discovered while
        #: installing the boundary and announced once the run can emit events
        self._unredactable: tuple[str, ...] = ()

    # -- the redaction boundary -------------------------------------------------------------

    def _install_redactor(self) -> None:
        """Make the store's redactor cover every value THIS run knows, before anything is written.

        The boundary must not depend on the caller having wired it. A store arrives with
        :data:`~rayspec.redact.NULL_REDACTOR` and an embedder following ``docs/extending.md``
        never assigns one, so a run that knows a secret installs what it needs itself: the
        ``secret: true`` inputs it was given plus the ``config.secrets`` values it hands to
        ``shell:``/``python:`` steps (:attr:`RunOptions.config_secrets`).

        A redactor the caller DID install is extended, never replaced — the CLI's also carries
        the opt-in detectors and values this run cannot see. A store that will not accept one
        makes the run refuse to start: a workflow with a secret input and no way to redact it
        must not write a single byte.

        The two sets of values are handed over as PAIRS, not merged into one mapping: an input
        name and a ``config.secrets`` name are independent namespaces that can collide, and
        merging by name would drop one of the two values from the redactor while
        ``process_env`` still exports both to the step. Everything the run knows goes through
        :meth:`~rayspec.redact.Redactor.extend`, including values already covered, so that a
        value too short to redact is recorded in ``skipped`` and can be announced — an embedded
        run must not be quieter than the CLI about the one case where redaction does nothing.
        """
        secrets = [*self.options.config_secrets.items(), *self.secret_inputs.items()]
        if not secrets:
            return
        current = getattr(self.store, "redactor", None)
        if current is None:
            current = NULL_REDACTOR
        updated = current.extend(secrets)
        if updated is current:  # the caller's redactor already knows every value and every name
            return
        try:
            self.store.redactor = updated
        except (AttributeError, TypeError) as exc:
            names = sorted({name for name, value in secrets if not current.covers(value)})
            raise EngineError(
                f"{type(self.store).__name__} does not accept a redactor, so the secret "
                f"value(s) {', '.join(names or sorted({n for n, _ in secrets}))} could not be "
                f"kept out of the run directory",
                hint="use a store whose `redactor` attribute can be assigned (every store "
                "rayspec builds can), or run a workflow without secret inputs",
            ) from exc
        self._unredactable = tuple(n for n in updated.skipped if n not in current.skipped)

    # -- entry points ---------------------------------------------------------------------

    def run_sync(self) -> RunResult:
        """``anyio.run(self.run, backend="asyncio")``."""
        return anyio.run(self.run, backend="asyncio")

    async def run(self) -> RunResult:
        """Execute (or resume) the workflow and return the :class:`RunResult`."""
        workflow = self.resolved.workflow
        runtime = Runtime(workflow.defaults.max_parallel)
        configure_default_executor(runtime.max_parallel)
        self._install_redactor()  # before the first byte is written, the lock file included
        self._acquire_lock()
        try:
            if self.resume_run_id:
                # the worktree may have moved on while paused — refresh before the record
                # is prepared (``workspace.info()``), off the event loop like the run-end path
                await to_thread.run_sync(self._refresh_head_sha)
            # the ``ps`` probe is a blocking subprocess call — keep it off the event loop
            pid_started_at = await to_thread.run_sync(process_start_time, os.getpid())
            # who is launching this — an account-database lookup, which a directory service
            # can make slow, so it stays off the event loop too; a resume keeps the actor the
            # record already carries and needs no lookup at all
            actor = None if self.resume_run_id else await to_thread.run_sync(self._resolve_actor)
            run, cache, hash_mismatch, resumed = self._prepare_record(pid_started_at, actor)
        except BaseException:
            self._release_lock()
            raise
        ctx = RunContext(
            resolved=self.resolved,
            run=run,
            store=self.store,
            sinks=self.sinks,
            engine=self.engine,
            runtime=runtime,
            options=self.options,
            workdir=self.workspace.workdir,
            project={
                "root": str(self.project_root),
                "name": self.project_name,
                "slug": self.project_slug,
            },
            env=self.env,
            approval_prompt=self.approval_prompt,
            providers=self.providers,
            cache=cache,
            hash_mismatch=hash_mismatch,
            secret_inputs=self.secret_inputs,
            envelope=self.envelope,
        )
        ctx.price_table = self.price_table
        ctx.executors.update(self.executors)
        self.ctx = ctx
        views: dict[str, StepView] = {}
        root_scope = ExecScope(
            prefix=StepPath.root(),
            def_prefix="",
            tscope=Scope(None, views),
            views=views,
            inputs=dict(run.inputs),  # redacted: secrets stand as "<secret>"
            defaults=workflow.defaults,
        )
        graph = StepGraph.from_steps(workflow.steps)
        outcomes: dict[str, StepOutcome] = {}
        await ctx.emit(
            EventType.RUN_RESUMED if resumed else EventType.RUN_STARTED,
            workflow=workflow.name,
            dry_run=self.options.dry_run,
            resume_count=run.resume_count,
            workdir=str(self.workspace.workdir),
        )
        if self._unredactable:
            # the CLI prints the same fact before the run starts; an embedder only has events
            await ctx.warn(
                f"{', '.join(self._unredactable)} is shorter than {MIN_REDACTABLE_LEN} "
                "characters and is therefore not redacted — it can appear in the run store, "
                "the logs and the console"
            )
        if self.workspace.isolation != "none":
            await ctx.emit(
                EventType.WORKSPACE_CREATED,
                workdir=str(self.workspace.workdir),
                branch=self.workspace.branch,
                base_sha=self.workspace.base_sha,
            )
        await self._consume_envelope_decision(ctx, run)
        if not resumed and run.toolchain is None:  # the SDK/CLI/models in effect, once
            run.toolchain = await capture_toolchain(ctx)
            await ctx.save_run()
        # a resumed run whose ``defaults.timeout_total`` already expired starts nothing: the
        # clock runs from the ORIGINAL start, so the breaker has to be asked before the graph
        await ctx.check_budget()

        engine_error: BaseException | None = None

        async def body() -> None:
            nonlocal engine_error
            try:
                outcomes.update(await run_graph(graph, root_scope, ctx))
            except RunStopped as exc:
                ctx.stopped = exc
            except RunPaused as exc:
                ctx.paused = ctx.paused or exc
            except anyio.get_cancelled_exc_class():
                raise
            except BaseExceptionGroup as group:
                inner = unwrap_exception_group(group)
                if isinstance(inner, RunStopped):
                    ctx.stopped = inner
                elif isinstance(inner, RunPaused):
                    ctx.paused = ctx.paused or inner
                elif isinstance(inner, anyio.get_cancelled_exc_class()):
                    raise
                else:  # a bug in the engine: finalize the run as failed, never leave it running
                    engine_error = inner
            except Exception as exc:
                engine_error = exc

        interrupted = False
        try:
            signal_result = await run_with_signals(
                body, on_hard_exit=ctx.save_run_sync, handle_signals=self.handle_signals
            )
            interrupted = signal_result.interrupted
        except anyio.get_cancelled_exc_class():
            interrupted = True
            with anyio.CancelScope(shield=True):
                await self._finalize(ctx, root_scope, outcomes, interrupted=True)
            raise
        result: RunResult | None = None
        with anyio.CancelScope(shield=True):
            result = await self._finalize(
                ctx, root_scope, outcomes, interrupted=interrupted, engine_error=engine_error
            )
        assert result is not None
        return result

    # -- internals ------------------------------------------------------------------------

    async def _publish_branch(self, ctx: RunContext) -> None:
        """Push the run's branch when ``RAYSPEC_PUSH_BRANCH`` asks for it — best effort.

        The point is that a run left alone on a schedule leaves its committed work somewhere
        visible: the branch is pushed when the run pauses and when it ends, so the laptop is not
        the only copy. Only an isolated run publishes — an in-place run is on the user's own
        branch and pushing that would be a surprise — and a dry run publishes nothing.

        A push moves commits, and rayspec commits nothing by itself: if the worktree still holds
        uncommitted work, that work did NOT leave the machine, and the run says so rather than
        letting a successful push read as "it is backed up".

        The push happens after the run's outcome is already decided, so it may never influence
        it: every failure (no remote, a rejected push, a timeout, no git at all) becomes a
        warning event on the finished run, and the status and the exit code are what they would
        have been without the hook.
        """
        from rayspec.loader.loader import import_optional

        module = import_optional("rayspec.workspace.git")
        push_remote = getattr(module, "push_remote", None) if module is not None else None
        push_branch = getattr(module, "push_branch", None) if module is not None else None
        if push_remote is None or push_branch is None:
            return
        workspace = self.workspace
        remote = push_remote()
        if (
            remote is None
            or self.options.dry_run
            or workspace.isolation == "none"
            or not workspace.branch
        ):
            return
        branch, workdir = workspace.branch, workspace.workdir
        try:
            outcome = await to_thread.run_sync(lambda: push_branch(workdir, branch, remote=remote))
        except Exception as exc:  # a hook must not be able to break a finished run
            await ctx.warn(f"could not push {branch} to {remote}: {exc}")
            return
        if not outcome.pushed:
            await ctx.warn(f"could not push {branch} to {remote}: {outcome.reason}")
        elif outcome.uncommitted:
            # a push publishes commits; rayspec makes none, so this work stayed on the machine
            await ctx.warn(
                f"pushed {branch} to {remote}, but {outcome.uncommitted} uncommitted change(s) "
                "in the worktree were not published"
            )

    def _resolve_actor(self) -> ActorInfo:
        """Who is launching this run (blocking: an account-database lookup can be slow).

        Resolved from the environment **as the operator set it** and from the operating-system
        user only — never from the workspace the run is about to write to, never from a git
        configuration (one ``shell:`` step rewrites one in any scope), and never from a variable
        rayspec copied out of a ``.env`` file (a step can write those files too, and the home
        one persists into every later run). See :func:`rayspec.actor.resolve_actor`.
        """
        return resolve_actor()

    async def _settle_envelope(
        self, ctx: RunContext, status: RunStatus, cost: float | None
    ) -> None:
        """Commit the run's final spend and move the consecutive-failure counter.

        Called on every final status, from a shielded scope, so a run that reached a ceiling is
        still counted even though the check itself stopped asking. A dry run spends nothing and
        is not counted; a PAUSED run is not an outcome yet, so the failure streak is left alone.

        A ledger that cannot be written loses this run's spend — a smaller failure than refusing
        to run at all, but never a silent one: the operator is told, because an envelope that
        quietly forgot a hundred dollars is worse than one that is simply absent.
        """
        envelope = self.envelope
        if envelope is None or ctx.options.dry_run:
            return
        try:
            await to_thread.run_sync(envelope.commit_final, cost)
            if status is RunStatus.SUCCEEDED:
                await to_thread.run_sync(_record_outcome, envelope, False)
            elif status is RunStatus.FAILED:
                await to_thread.run_sync(_record_outcome, envelope, True)
        except OSError as exc:
            await ctx.warn(
                f"the spend ledger could not be written ({exc}) — this run's spend and its "
                "outcome are not in the operator's totals"
            )
        for problem in envelope.take_warnings():
            await ctx.warn(problem)

    async def _refresh_envelope_pause(self, ctx: RunContext, cost: float | None) -> None:
        """Re-phrase a ceiling pause from the run's FINAL totals.

        The message is the operator's record of how far over the ceiling the run went, and it is
        the number they decide on — so it must be the amount actually spent, not the amount at
        the moment the check first tripped.
        """
        envelope = self.envelope
        if envelope is None or ctx.envelope_pause is None or ctx.options.dry_run:
            return
        try:
            reason = await to_thread.run_sync(envelope.settle, cost)
        except OSError:
            return  # _settle_envelope reports it; the first phrasing stands
        if reason is not None:
            ctx.envelope_pause = reason
            ctx.envelope_pause_kind = envelope.pause_kind

    async def _consume_envelope_decision(self, ctx: RunContext, run: RunRecord) -> None:
        """Apply the decision an operator recorded on a ceiling pause, then clear it.

        Any resume entry clears the pause — the envelope is evaluated again from scratch, so a
        run continued after the ceiling was raised simply proceeds and one continued while it is
        still exceeded pauses again with a fresh pause. ``rayspec approve <run>`` additionally
        means "I have looked, run it anyway": the ceilings stop stopping THIS run. The
        consecutive-failure breaker is only closed when the breaker is what stopped the run —
        approving a spend is not approving a failure streak, and the console says which of the
        two the approval covered. ``rayspec reject`` means "no" — the decision is dropped and
        the run pauses again on the same ceiling, which is the honest outcome, because nothing
        about the ceiling was changed.
        """
        pause = run.pause
        if pause is None or pause.reason not in OPERATIONAL_PAUSE_REASONS:
            return
        approved = pause.decision is not None and pause.decision.approved
        breaker = pause.reason == FAILURE_PAUSE_REASON
        run.pause = None
        if not approved or self.envelope is None:
            return
        self.envelope.waive(close_breaker=breaker)
        await ctx.warn(
            "the consecutive-failure breaker is closed again for this project "
            "(the spending ceilings are not waived)"
            if breaker
            else "the spending ceilings are waived for this run (the failure breaker is not)"
        )

    def _acquire_lock(self) -> None:
        """Take the workdir path lock (non-blocking) when a home is known.

        Skipped for pure dry runs (nothing touches the workdir) and where ``rayspec.workspace``
        or ``fcntl`` is unavailable. A held lock raises :class:`EngineError` (exit 2) naming the
        holder — two runs never share a working directory.
        """
        if self.home is None or (self.options.dry_run and not self.options.exec_shell):
            return
        from rayspec.loader.loader import import_optional

        module = import_optional("rayspec.workspace")
        lock_cls = getattr(module, "PathLock", None) if module is not None else None
        if lock_cls is None:
            return
        lock = lock_cls(
            self.home,
            self.project_slug,
            self.workspace.workdir,
            run_id=self.resume_run_id or self.run_id or "",
        )
        try:
            lock.acquire()
        except NotImplementedError:  # no fcntl (Windows): run unguarded
            return
        except Exception as exc:
            hint = getattr(exc, "hint", None)
            raise EngineError(str(exc), hint=hint) from exc
        self._lock = lock

    def _release_lock(self) -> None:
        if self._lock is not None:
            with contextlib.suppress(Exception):
                self._lock.release()
            self._lock = None

    def _refresh_head_sha(self) -> None:
        """Re-read ``HEAD`` of the run workdir into ``workspace.head_sha``.

        ``head_sha`` is "the tip of the run workdir at the last record write": refreshed at pause,
        at run end and on resume start, so ``rayspec show`` prints the agent's latest commit
        rather than the sha the worktree was created from. Only for git workdirs (a recorded
        ``branch``/``head_sha``); best effort — git errors leave the previous value.
        """
        ws = self.workspace
        if ws.branch is None and ws.head_sha is None:
            return
        from rayspec.loader.loader import import_optional

        module = import_optional("rayspec.workspace.git")
        rev_parse = getattr(module, "rev_parse", None) if module is not None else None
        if rev_parse is None:
            return
        try:
            ws.head_sha = str(rev_parse(ws.workdir))
        except Exception:
            return

    def _prepare_record(
        self, pid_started_at: str | None = None, actor: ActorInfo | None = None
    ) -> tuple[RunRecord, dict[str, StepRecord], bool, bool]:
        """Create a fresh record or load the one to resume → (run, cache, mismatch, resumed).

        ``pid_started_at`` is this process's start time (:func:`process_start_time`, computed by
        the caller off the event loop), recorded next to ``pid`` for ``rayspec cancel``.
        ``actor`` is who launched the run (also resolved off the event loop — an account-database
        lookup can be slow); it is stamped on a FRESH record only, so a resume by somebody else
        never rewrites who started the run.
        """
        workflow = self.resolved.workflow
        if self.resume_run_id:
            run = self.store.load(self.resume_run_id)
            if run.workflow_name != workflow.name:
                # never resume a run under another workflow (--force does not cross workflows)
                raise ResumeError(
                    f"run {run.run_id} belongs to workflow {run.workflow_name!r}, "
                    f"not {workflow.name!r}",
                    hint=f"use `rayspec resume {run.run_id}` (it reloads the run's own workflow)",
                )
            if run.status is RunStatus.RUNNING and not self.options.force:
                if _on_other_host(run):
                    raise ResumeError(
                        f"run {run.run_id} is recorded as running on host {run.host} "
                        f"(pid {run.pid or '?'}) — its process cannot be checked from here",
                        hint="resume it on that host, or pass --force if you are sure it is dead",
                    )
                if _pid_alive(run):
                    raise ResumeError(
                        f"run {run.run_id} is still running (pid {run.pid} on {run.host})",
                        hint=f"stop it first with `rayspec cancel {run.run_id}`, "
                        "or pass --force if you are sure it is dead",
                    )
            mismatch = run.workflow_hash != self.resolved.hash
            if mismatch and not self.options.force:
                raise ResumeError(
                    f"workflow {workflow.name!r} changed since run {run.run_id} "
                    f"(hash {run.workflow_hash[:12]} → {self.resolved.hash[:12]})",
                    hint="pass --force to resume anyway (changed steps are re-run)",
                )
            missing = [
                name
                for name in secret_input_names(workflow)
                if run.inputs.get(name) == SECRET_PLACEHOLDER and name not in self.secret_inputs
            ]
            if missing:
                # secrets are never persisted — every resume entry must supply them again
                raise ResumeError(
                    "missing secret input(s): " + ", ".join(missing),
                    hint="pass "
                    + " ".join(f"--input {n}=…" for n in missing)
                    + " or set "
                    + ", ".join(env_var_name(n) for n in missing),
                )
            for name in secret_input_names(workflow):
                # an optional secret not given at launch may be supplied now — recorded
                # as the placeholder so that it is exported (and required) like the others
                if name in self.secret_inputs and name not in run.inputs:
                    run.inputs[name] = SECRET_PLACEHOLDER
            cache = dict(run.steps)
            if self.options.stubs_path is not None:
                run.stubs_path = self.options.stubs_path  # --stubs on resume overrides
            run.secret_inputs = tuple(secret_input_names(workflow))
            run.status = RunStatus.RUNNING
            run.reason = None
            run.ended_at = None
            run.outputs = None
            run.resume_count += 1
            run.pid = os.getpid()
            run.pid_started_at = pid_started_at  # a new process: refresh
            run.host = socket.gethostname()
            run.started_at = run.started_at or utcnow()
            run.workflow_hash = self.resolved.hash
            run.dry_run = bool(self.options.dry_run)
            run.workspace = self.workspace.info()  # head_sha refreshed by ``run()``
            self.store.save(run)
            return run, cache, mismatch, True
        run = RunRecord(
            run_id=self.run_id or new_run_id(),
            workflow_name=workflow.name,
            workflow_path=self.resolved.label,
            workflow_hash=self.resolved.hash,
            project_slug=self.project_slug,
            project_root=str(self.project_root),
            inputs=redact_inputs(
                {**self.inputs, **self.secret_inputs}, secret_input_names(workflow)
            ),
            secret_inputs=tuple(secret_input_names(workflow)),
            stubs_path=self.options.stubs_path,
            status=RunStatus.RUNNING,
            started_at=utcnow(),
            pid=os.getpid(),
            pid_started_at=pid_started_at,
            host=socket.gethostname(),
            workspace=self.workspace.info(),
            dry_run=bool(self.options.dry_run),
            # who set this run going — resolved once, at the first start, and never rewritten
            # by a resume (``_prepare_record`` returns early above for a resumed run)
            actor=actor,
        )
        self.store.create(run)
        return run, {}, False, False

    async def _finalize(
        self,
        ctx: RunContext,
        root_scope: ExecScope,
        outcomes: dict[str, StepOutcome],
        *,
        interrupted: bool,
        engine_error: BaseException | None = None,
    ) -> RunResult:
        run = ctx.run
        workflow = self.resolved.workflow
        status: RunStatus
        reason: str | None = None
        outputs: dict[str, Any] | None = None
        # Computed BEFORE the control-signal ladder: an untolerated failure is a fact about the
        # run that no later signal may erase. This matters only since `on_step_failure: continue`:
        # under `drain` a control step on another branch was unreachable after a failure,
        # so `ctx.stopped` could safely decide the status. With the ready-set left open, a
        # `stop: {status: succeeded}` would otherwise report exit 0 and publish `outputs:` for a
        # run whose `run.json` holds a failed step.
        # Read the PERSISTED records, not ``outcomes``: when a control signal propagates out of
        # ``run_graph`` the caller never reaches ``outcomes.update(...)``, so that dict is empty
        # exactly in the case this guard exists for.
        #
        # TOP-LEVEL ONLY (``StepPath`` depth 1). Composite steps roll their bodies up under their
        # own policy — ``each.on_failure: continue`` tolerates a failed item, ``loop.on_exhausted``
        # decides an exhausted loop — and those nested records stay ``tolerated=False`` in the
        # store. Counting them here would fail a run whose ``each:`` step legitimately succeeded.
        failed = [r for r in failed_steps(run) if len(StepPath.parse(r.path).segments) == 1]
        if ctx.stopped is not None:
            # The step that RAISED the stop must not outrank its own signal: `on_reject: cancel`
            # records the gate as REJECTED *and* stops the run, and that is a deliberate cancel
            # (exit 4), not a failure. Only a failure somewhere ELSE means the stop is laundering.
            failed = [r for r in failed if r.path != ctx.stopped.step_path]

        def _failure_reason() -> str:
            first = failed[0]
            msg = first.error.message if first.error else first.status.value
            return f"step {first.path!r} {first.status.value}: {msg}"

        usage, cost, cost_source = ctx.run_totals()
        # the ceiling's wording is refreshed from the final totals before it becomes the run's
        # reason: a drain can spend more after the check first tripped
        await self._refresh_envelope_pause(ctx, cost)
        if engine_error is not None:
            status = RunStatus.FAILED
            reason = f"engine error: {type(engine_error).__name__}: {engine_error}"
        elif interrupted and ctx.paused is None:
            status = RunStatus.INTERRUPTED
            reason = "interrupted"
        elif ctx.paused is not None:
            # the run is not finished, so PAUSED stands — but name the failure so nobody is asked
            # to authorise a gate on a run that has already failed (resume ends it FAILED)
            status = RunStatus.PAUSED
            reason = f"awaiting approval at {ctx.paused.step_path}"
            if failed:
                reason += f" ({len(failed)} step(s) already failed: {_failure_reason()})"
        elif ctx.envelope_pause is not None:
            # an OPERATIONAL ceiling (policy budget / circuit breaker), not a workflow defect:
            # the run stopped so a person can look at it. Ranked above ``budget_exceeded``
            # because the envelope sets that flag too (it is what makes the run drain).
            status = RunStatus.PAUSED
            reason = ctx.envelope_pause
            if failed:
                reason += f" ({len(failed)} step(s) already failed: {_failure_reason()})"
        elif ctx.budget_exceeded is not None:
            # the run-level cap tripped — failed with the cap as the reason (exit 1);
            # resumable once the cap is raised (replayed records count towards it again).
            # Ranked ABOVE ``stopped``: a run that blew its cost, token or time cap keeps
            # draining, so it reaches a ``join: always`` ``stop: {status: succeeded}`` (the
            # finally idiom) — and a capped run must never report success to its caller.
            status = RunStatus.FAILED
            reason = ctx.budget_exceeded
        elif ctx.stopped is not None and failed:
            status = RunStatus.FAILED
            reason = _failure_reason()
        elif ctx.stopped is not None:
            status = RunStatus(ctx.stopped.status)
            reason = ctx.stopped.reason or f"stopped by {ctx.stopped.step_path}"
        elif failed:
            status = RunStatus.FAILED
            reason = _failure_reason()
        else:
            status = RunStatus.SUCCEEDED
        if status is RunStatus.SUCCEEDED and workflow.outputs:
            try:
                outputs = ctx.engine.render_value(
                    dict(workflow.outputs), ctx.template_context(root_scope)
                )
            except TemplateRenderError as exc:
                status = RunStatus.FAILED
                reason = f"outputs: {exc}" + (f" (fix: {exc.hint})" if exc.hint else "")
                outputs = None
        elif status is RunStatus.SUCCEEDED:
            outputs = {}
        if ctx.envelope_pause is not None and run.pause is None:
            kind = ctx.envelope_pause_kind
            run.pause = PauseInfo(
                token=f"{kind}#{run.resume_count}",
                step=ctx.last_finished_path or ENVELOPE_PAUSE_STEP,
                message=ctx.envelope_pause,
                reason=kind,
            )
            await ctx.emit(
                EventType.RUN_PAUSED,
                step_path=run.pause.step,
                token=run.pause.token,
                step=run.pause.step,
                message=run.pause.message,
                reason=kind,
            )
        run.status = status
        run.reason = reason
        run.outputs = outputs
        run.ended_at = utcnow()
        if status is not RunStatus.PAUSED:
            run.pid = None
        await to_thread.run_sync(self._refresh_head_sha)  # pause / run end
        run.workspace = self.workspace.info()
        await self._publish_branch(ctx)  # opt-in, best effort — never changes ``status``
        run.cost_source = cost_source
        await self._settle_envelope(ctx, status, cost)
        self._release_lock()  # released on every final status — a resume takes it again
        data: dict[str, Any] = {
            "status": status.value,
            "reason": reason,
            "usage": {
                "input": usage.input,
                "cached_input": usage.cached_input,
                "cache_write": usage.cache_write,
                "output": usage.output,
                "reasoning": usage.reasoning,
            },
            "cost_usd": cost,
            "outputs": outputs,
        }
        if cost_source != "none":
            data["cost_source"] = cost_source
        try:
            await ctx.save_run()
            await ctx.emit(EventType.RUN_FINISHED, **data)
        finally:
            await ctx.providers.aclose()
        return RunResult(
            run_id=run.run_id,
            status=status,
            exit_code=exit_code_for(status),
            run_dir=ctx.run_dir,
            workspace=self.workspace,
            outputs=outputs,
            reason=reason,
            usage=usage,
            cost_usd=cost,
            cost_source=cost_source,
            steps=dict(run.steps),
            reused=list(ctx.reused_paths),
            pause=run.pause,
            interrupted=interrupted,
            record=run,
        )


def _record_outcome(envelope: Any, failed: bool) -> None:
    """``RunEnvelope.record_outcome`` as a positional call (``to_thread`` takes no kwargs)."""
    envelope.record_outcome(failed=failed)


def _on_other_host(run: RunRecord) -> bool:
    """Whether ``run.host`` names another machine (shared ``RAYSPEC_HOME``): unknowable liveness."""
    return bool(run.host) and run.host != socket.gethostname()


#: Linux process table (overridable in tests).
_PROC_ROOT = Path("/proc")


#: Environment for the ``ps`` probe: the ``lstart`` string depends on the locale (``Do. 20 Aug.``
#: vs ``Thu Aug 20``) and on ``TZ``, and the engine (launch) and ``rayspec cancel`` may run under
#: different shells (cron/CI vs interactive) — pin both so the two sides always agree.
_PS_ENV = {"LC_ALL": "C", "TZ": "UTC"}


def _ps_lstart(pid: int, *, timeout_s: float) -> str | None:
    """``ps -o lstart= -p <pid>`` under ``LC_ALL=C TZ=UTC``, stripped; ``None`` when the process
    is gone or ``ps`` fails (non-zero exit, e.g. a busybox ``ps`` without ``-o lstart``).

    Raises :class:`FileNotFoundError` when there is no ``ps`` at all and
    :class:`subprocess.TimeoutExpired` when it hangs (the caller falls back to ``/proc``).
    """
    proc = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        env={**os.environ, **_PS_ENV},
    )
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip()
    return line or None


def _proc_starttime(pid: int, *, proc_root: Path | None = None) -> str | None:
    """Field 22 (``starttime``, clock ticks since boot) of ``/proc/<pid>/stat`` — Linux only.

    The ``comm`` field (2) is parenthesised and may contain spaces and parentheses, so the fields
    are counted from the LAST ``)``. ``None`` when the file is missing or malformed.
    """
    root = _PROC_ROOT if proc_root is None else proc_root
    try:
        text = (root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _, sep, rest = text.rpartition(")")
    if not sep:
        return None
    fields = rest.split()
    # ``rest`` starts at field 3 (state): starttime is field 22 → index 19
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return fields[19]


def process_start_time(pid: int, *, timeout_s: float = 5.0) -> str | None:
    """The start time of process ``pid`` as an opaque string to compare for equality.

    The ``ps -o lstart= -p <pid>`` output run under a fixed environment (``LC_ALL=C TZ=UTC``, e.g.
    ``Thu Aug 20 12:00:00 2026``; one-second resolution) — so the string is the same for the same
    process whichever shell, locale or timezone the caller has; when ``ps`` is missing, fails
    (busybox without ``-o lstart``) or hangs, the ``/proc/<pid>/stat`` ``starttime`` field on
    Linux. ``None`` for a pid that does not exist, an invalid pid, or when neither source can be
    read — callers treat "unknown" as "not verified". The engine records it next to ``pid`` in
    ``run.json`` at launch and on resume; ``rayspec cancel`` compares it with the live process.
    """
    if pid <= 0:
        return None
    value: str | None = None
    try:
        value = _ps_lstart(pid, timeout_s=timeout_s)
    except (OSError, subprocess.SubprocessError):
        value = None
    if value is not None:
        return value
    return _proc_starttime(pid)


def _pid_alive(run: RunRecord) -> bool:
    """Whether the process recorded in ``run.json`` still exists (same host only; POSIX)."""
    if run.pid is None or run.pid <= 0 or (run.host and run.host != socket.gethostname()):
        return False
    try:
        os.kill(run.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _null_sink() -> EventSink:
    from rayspec.events.sinks import NullSink

    return NullSink()


def failed_steps(run: RunRecord) -> list[StepRecord]:
    """Untolerated failed/interrupted/rejected records of a run (for summaries)."""
    return [r for r in run.steps.values() if r.status in FAILED_LIKE and not r.tolerated]


__all__ = [
    "RunResult",
    "Runner",
    "Workspace",
    "failed_steps",
    "fallback_project_slug",
    "process_start_time",
]
