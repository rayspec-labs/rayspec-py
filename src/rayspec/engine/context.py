# SPDX-License-Identifier: Apache-2.0
"""Shared run state handed to the scheduler and the executors.

Module boundary: the glue between the core modules (store, events, templating, providers
registry) and the engine proper. It owns

* :class:`RunOptions` — the run-level switches (dry run, ``--yes``, fail-fast, force …);
* :class:`ExecScope` — one sibling list's execution scope: record-path prefix (indexed, e.g.
  ``build[2]``), definition-path prefix (un-indexed, what the loader uses), the templating
  :class:`~rayspec.templating.Scope`, the inputs visible here and the composite metadata
  (iteration / item index + sha) stamped onto records;
* :class:`StepOutcome` — a step's record plus its in-memory output and any control signal;
* :class:`RunContext` — store + sinks + templating + providers + runtime + the resume cache,
  with the write-ahead ``persist`` (output file → record → run.json) and the event helpers.
  The fsync-backed store calls (``save``, ``write_output*``) run in a worker thread
  (``anyio.to_thread``) under one ``anyio.Lock`` so they never stall the event loop yet stay
  ordered; JSONL appends (events, streams) only flush and stay inline.

Nothing here schedules or executes steps.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from anyio import to_thread

from rayspec.engine.approval import ApprovalPrompt
from rayspec.engine.approval_classes import ApprovalClasses
from rayspec.engine.errors import RunControl, RunPaused, RunStopped
from rayspec.engine.paths import StepPath
from rayspec.engine.runtime import Runtime
from rayspec.events.base import EventSink
from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.fmt import humanize_duration
from rayspec.loader import ResolvedWorkflow
from rayspec.providers.base import Provider, Usage
from rayspec.providers.pricing import combine_cost_sources
from rayspec.redact import Redactor
from rayspec.schema import Defaults, EachStep, LoopStep, StepModel, StepStatus
from rayspec.store.base import RunStore
from rayspec.store.model import ArtifactRef, ErrorInfo, RunRecord, StepRecord, utcnow
from rayspec.templating import (
    Scope,
    StepView,
    TemplateEngine,
    build_context,
    stringify_scalar,
    stringify_text,
)

log = logging.getLogger("rayspec.engine")

#: Step kinds whose records may be replayed on resume (composites replay their bodies instead).
REUSABLE_KINDS: frozenset[str] = frozenset({"prompt", "shell", "python", "approve"})
LEAF_KINDS: frozenset[str] = frozenset({"prompt", "shell", "python"})
#: ``skip_reason`` of steps that were not started because the run-level cap tripped.
BUDGET_SKIP_REASON = "budget_exceeded"

#: How much of an artifact is read at a time when the store keeps no copy of it.
_ARTIFACT_CHUNK_BYTES = 1 << 20


#: The run-level caps in the order the breaker reports them — the documented precedence. The
#: cost and token caps are one budget and are reported as one sentence; the wall clock follows.
CAP_KNOBS: tuple[str, ...] = (
    "defaults.budget_usd",
    "defaults.max_tokens",
    "defaults.timeout_total",
)
#: How a run-level breaker reason starts (``RunRecord.reason`` of a run a cap ended).
CAP_REASON_PREFIXES: tuple[str, ...] = ("budget exceeded (", "time limit exceeded (")


@dataclass(frozen=True, slots=True)
class CapBreach:
    """One run-level cap the run is over: the knobs to raise, and the one-line reason."""

    knobs: tuple[str, ...]
    reason: str


def budget_parts(
    usage: Any, cost_usd: float | None, cost_source: str, defaults: Defaults
) -> list[tuple[str, str]]:
    """``(knob, clause)`` for each money cap of ``defaults`` the totals exceed.

    Cost is the provider-reported figure or the pricing-table estimate (``~$``); a run without any
    known cost cannot trip ``budget_usd`` (tokens are always known).
    """
    parts: list[tuple[str, str]] = []
    cap_cost = defaults.budget_usd
    if cap_cost is not None and cost_usd is not None and cost_usd > cap_cost:
        approx = {"table": "~", "partial": "≥"}.get(cost_source, "")
        parts.append(
            ("defaults.budget_usd", f"cost {approx}${cost_usd:.3f} > budget_usd ${cap_cost:.3f}")
        )
    cap_tokens = defaults.max_tokens
    total = int(getattr(usage, "total", 0) or 0)
    if cap_tokens is not None and total > cap_tokens:
        parts.append(("defaults.max_tokens", f"tokens {total:,} > max_tokens {cap_tokens:,}"))
    return parts


def budget_reason(
    usage: Any, cost_usd: float | None, cost_source: str, defaults: Defaults
) -> str | None:
    """``budget exceeded (cost ~$0.40 > budget_usd $0.30, tokens 12,000 > max_tokens 10,000)``
    when a money cap of ``defaults`` is exceeded by the totals, else ``None``.
    """
    parts = budget_parts(usage, cost_usd, cost_source, defaults)
    if not parts:
        return None
    return f"budget exceeded ({', '.join(clause for _, clause in parts)})"


def cap_reasons(
    usage: Any,
    cost_usd: float | None,
    cost_source: str,
    elapsed_s: float | None,
    defaults: Defaults,
) -> tuple[CapBreach, ...]:
    """Every run-level cap the run is over, in :data:`CAP_KNOBS` order.

    The precedence is written down rather than merely deterministic: the money caps
    (``budget_usd`` / ``max_tokens``, one sentence because they are one budget) come before the
    wall clock (``timeout_total``), so the first breach is the primary reason. ALL of them are
    reported — a cap that fired must never go unnamed because another one fired too, which is
    what a step's ``skip_reason: budget_exceeded`` on its own cannot tell anybody.
    """
    breaches: list[CapBreach] = []
    money = budget_parts(usage, cost_usd, cost_source, defaults)
    if money:
        breaches.append(
            CapBreach(
                knobs=tuple(knob for knob, _ in money),
                reason=f"budget exceeded ({', '.join(clause for _, clause in money)})",
            )
        )
    clock = time_reason(elapsed_s, defaults)
    if clock is not None:
        breaches.append(CapBreach(knobs=("defaults.timeout_total",), reason=clock))
    return tuple(breaches)


def is_cap_reason(reason: str | None) -> bool:
    """Whether ``reason`` is what the run-level breaker writes (``RunRecord.reason``)."""
    return reason is not None and reason.startswith(CAP_REASON_PREFIXES)


def time_reason(elapsed_s: float | None, defaults: Defaults) -> str | None:
    """``time limit exceeded (elapsed 2h 1m > timeout_total 2h 0m)`` when the run has been
    going longer than ``defaults.timeout_total``, else ``None``.

    The third run-level circuit breaker beside :func:`budget_reason`: same trip rule (strictly
    greater), same consequence (no new step starts, running ones drain, the run ends failed).
    ``elapsed_s`` is measured from the run's original start, so it survives a resume.
    """
    cap = defaults.timeout_total
    if cap is None or elapsed_s is None or elapsed_s <= cap:
        return None
    return (
        f"time limit exceeded (elapsed {humanize_duration(elapsed_s * 1000)} "
        f"> timeout_total {humanize_duration(cap * 1000)})"
    )


def cost_source_of(records: Iterable[StepRecord]) -> str:
    """Run-level cost source over ``records`` (pinned seam):

    * ``none`` — no record has a cost at all (a stub/dry run shows tokens only);
    * ``partial`` — at least one record has tokens but no cost (an unpriced provider without a
      pricing-table entry): the summed cost is a lower bound (``≥$``);
    * ``table`` — at least one cost is a pricing-table estimate and none is unknown (``~$``);
    * ``provider`` — every record with tokens reported a provider cost (``$``).

    Records without tokens and without cost (shell/python, skipped steps) do not count.

    The fold itself is :func:`~rayspec.providers.pricing.combine_cost_sources` — the one place
    the four sources are combined; this function only says which records take part. A record
    that HAS a cost but names no source is read as ``provider``: a cost that was reported is
    never an estimate.
    """
    records = list(records)
    return combine_cost_sources(
        [
            rec.cost_source if rec.cost_source and rec.cost_source != "none" else "provider"
            for rec in records
            if rec.cost_usd is not None
        ],
        unpriced=any(rec.cost_usd is None and rec.usage.total for rec in records),
    )


def totals_of(records: Iterable[StepRecord]) -> tuple[Usage, float | None, str]:
    """``(usage, cost_usd, cost_source)`` summed over ``records`` (see :func:`cost_source_of`);
    ``cost_usd`` is the sum of the known costs (``None`` when no record has one)."""
    records = list(records)
    usage = Usage()
    costs: list[float] = []
    for rec in records:
        usage = usage + rec.usage
        if rec.cost_usd is not None:
            costs.append(rec.cost_usd)
    return usage, (sum(costs) if costs else None), cost_source_of(records)


@dataclass(slots=True)
class RunOptions:
    """Run-level switches (from the CLI flags)."""

    dry_run: bool = False
    exec_shell: bool = False
    yes: bool = False
    interactive: bool = True
    fail_fast: bool = False
    force: bool = False
    resume: bool = False
    stub_script: Any = None  # StubScript | Mapping | None (dry run / --stubs)
    provider_settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: absolute path of the ``--stubs`` file: recorded in ``run.json`` at launch, and on a
    #: resume replaces the recorded one when given (``None`` keeps the record's value)
    stubs_path: str | None = None
    #: additive: ``config.secrets`` resolved once at run start, ``NAME`` → value. Handed
    #: ONLY to ``shell:``/``python:`` steps as environment variables (see
    #: ``engine.executors._process.process_env``); never persisted, never templated, never in a
    #: fingerprint. Empty for every run without a ``secrets:`` block.
    config_secrets: Mapping[str, str] = field(default_factory=dict)
    #: additive: the approval-class rules in force (from the operator's policy file) plus the
    #: classes ``--approve-class`` pre-authorised. The default permits everything, which is the
    #: behaviour of every run that names no class. See
    #: :mod:`rayspec.engine.approval_classes`.
    approval_classes: ApprovalClasses = field(default_factory=ApprovalClasses)


@dataclass(slots=True)
class StepOutcome:
    """A step's record plus the in-memory output value the templates see.

    ``control`` carries a :class:`RunStopped`/:class:`RunPaused` that the scheduler must bubble
    after the record has been persisted; ``reused`` marks a resume replay.
    """

    record: StepRecord
    output: Any = None
    output_kind: str | None = None  # "text" | "json" | None
    stderr: str | None = None
    items: list[dict[str, Any]] | None = None
    control: RunControl | None = None
    reused: bool = False
    event_data: dict[str, Any] = field(default_factory=dict)


def mark_failed(record: StepRecord, error: ErrorInfo) -> StepRecord:
    """Stamp ``failed`` + ``ok=False`` + ``error`` on ``record`` and return it.

    The one place those three fields are set together: a record left ``ok=None`` or without its
    error is a step whose failure the store cannot explain afterwards.
    """
    record.status = StepStatus.FAILED
    record.ok = False
    record.error = error
    return record


def failed_outcome(record: StepRecord, error: ErrorInfo, *, output: Any = None) -> StepOutcome:
    """:func:`mark_failed` plus the :class:`StepOutcome` the scheduler expects back.

    ``output`` is what the step produced before it failed (an agent's partial answer); it is
    carried as text so the templates and the store see it, exactly as for a succeeded step.
    """
    return StepOutcome(
        record=mark_failed(record, error),
        output=output,
        output_kind="text" if output else None,
    )


#: ``defaults.on_step_failure`` from the most permissive to the most restrictive. A failure
#: policy is a blast-radius control, so it may only ever be TIGHTENED from the outside in:
#: ``continue`` keeps scheduling after a failure, ``drain`` launches nothing new, ``fail_fast``
#: also cancels what is already running.
ON_STEP_FAILURE_ORDER: tuple[str, ...] = ("continue", "drain", "fail_fast")


def strictest_on_step_failure(*policies: str) -> str:
    """The most restrictive of ``policies`` under :data:`ON_STEP_FAILURE_ORDER`."""
    return max(policies, key=ON_STEP_FAILURE_ORDER.index)


def stated_on_step_failure(defaults: Defaults) -> str | None:
    """``defaults.on_step_failure`` when the workflow WROTE the key, else ``None``.

    Writing the key is a statement; not writing it accepts the ``drain`` default. The two are
    not the same thing, which is why this reads ``model_fields_set`` rather than the value.
    """
    if "on_step_failure" not in defaults.model_fields_set:
        return None
    return defaults.on_step_failure


def on_step_failure_floor(defaults: Defaults, parent: ExecScope | None) -> str:
    """The strictest policy this scope or any enclosing one STATED — the floor nesting may not
    go below. ``continue`` (the bottom of :data:`ON_STEP_FAILURE_ORDER`) means none did."""
    stated = stated_on_step_failure(defaults) or ON_STEP_FAILURE_ORDER[0]
    below = ON_STEP_FAILURE_ORDER[0] if parent is None else parent.on_step_failure_floor
    return strictest_on_step_failure(stated, below)


def effective_on_step_failure(defaults: Defaults, parent: ExecScope | None) -> str:
    """The failure policy in force for a scope whose defaults are ``defaults``.

    ``defaults.on_step_failure`` is lexically scoped, like ``defaults.timeout`` and unlike the
    run-wide ``defaults.max_parallel``: an ``include:``d workflow that *states* a policy governs
    its own body, and one that says nothing inherits the including run's. ``loop:``/``each:``
    bodies share their parent's defaults, so they always inherit.

    A stated policy may only ever TIGHTEN what an enclosing workflow stated
    (:data:`ON_STEP_FAILURE_ORDER`, :func:`on_step_failure_floor`). ``on_step_failure: fail_fast``
    on the root workflow says "when something fails, stop launching agents and shell steps at
    once", and that is a guarantee about the whole run: a vendored block writing ``continue`` must
    not be able to take it away and keep spending tokens in somebody else's workspace. The floor
    is what enclosing scopes *stated*, not what was in force for them — a run that never mentions
    the key has asked for nothing, so a block may still state ``continue`` for its own body. The
    ``--fail-fast`` flag is outside this scoping and tightens every scope at once
    (:meth:`RunContext.fail_fast_for`).
    """
    stated = stated_on_step_failure(defaults)
    if parent is None:
        return defaults.on_step_failure
    if stated is None:
        return parent.on_step_failure
    return strictest_on_step_failure(parent.on_step_failure_floor, stated)


class ExecScope:
    """Execution scope of one sibling list (root, loop iteration, each item, include body)."""

    def __init__(
        self,
        *,
        prefix: StepPath,
        def_prefix: str,
        tscope: Scope,
        views: dict[str, StepView],
        inputs: Mapping[str, Any],
        defaults: Defaults,
        iteration: int | None = None,
        item_index: int | None = None,
        item_sha256: str | None = None,
        parent: ExecScope | None = None,
    ) -> None:
        self.prefix = prefix
        self.def_prefix = def_prefix
        self.tscope = tscope
        self.views = views
        self.inputs = inputs
        self.defaults = defaults
        self.iteration = iteration
        self.item_index = item_index
        self.item_sha256 = item_sha256
        self.parent = parent
        #: why running steps were cancelled (set by the scheduler before cancelling a graph)
        self.cancel_reason: str | None = None
        #: the strictest policy this scope or an enclosing one STATED — see
        #: :func:`on_step_failure_floor`
        self.on_step_failure_floor: str = on_step_failure_floor(defaults, parent)
        #: ``drain`` | ``fail_fast`` | ``continue`` for THIS sibling list — see
        #: :func:`effective_on_step_failure`
        self.on_step_failure: str = effective_on_step_failure(defaults, parent)

    # -- paths ----------------------------------------------------------------------------

    def record_path(self, step_id: str) -> StepPath:
        """Indexed record path of a step of this graph (``build[2]/implement``)."""
        return self.prefix.child(step_id)

    def def_path(self, step_id: str) -> str:
        """Un-indexed definition path used by the loader (``build/implement``)."""
        return f"{self.def_prefix}{step_id}"

    def child(
        self,
        *,
        prefix: StepPath,
        def_prefix: str,
        variables: Mapping[str, Any] | None = None,
        inputs: Mapping[str, Any] | None = None,
        defaults: Defaults | None = None,
        iteration: int | None = None,
        item_index: int | None = None,
        item_sha256: str | None = None,
        lexical_root: bool = False,
    ) -> ExecScope:
        """A nested scope (loop iteration / each item / include body).

        ``lexical_root`` starts a fresh templating scope chain (include bodies must not see
        the including workflow's steps or variables).
        """
        views: dict[str, StepView] = {}
        tscope = (
            Scope(None, views, variables) if lexical_root else self.tscope.child(views, variables)
        )
        return ExecScope(
            prefix=prefix,
            def_prefix=def_prefix,
            tscope=tscope,
            views=views,
            inputs=self.inputs if inputs is None else inputs,
            defaults=self.defaults if defaults is None else defaults,
            iteration=self.iteration if iteration is None else iteration,
            item_index=self.item_index if item_index is None else item_index,
            item_sha256=self.item_sha256 if item_sha256 is None else item_sha256,
            parent=self,
        )


ExecutorFn = Callable[[StepModel, ExecScope, "RunContext", StepRecord, int], Awaitable[StepOutcome]]


class ProviderPool:
    """Per-run provider instances: created via the registry (or injected), opened once, closed
    together at the end of the run. In dry-run mode every provider id maps to the stub."""

    def __init__(self, ctx: RunContext, overrides: Mapping[str, Provider] | None = None) -> None:
        self.ctx = ctx
        self._instances: dict[str, Provider] = dict(overrides or {})
        self._opened: set[str] = set()
        self._lock = anyio.Lock()

    def key_for(self, provider_id: str) -> str:
        return (
            "stub"
            if self.ctx.options.dry_run and provider_id not in self._instances
            else provider_id
        )

    async def get(self, provider_id: str) -> Provider:
        """The (opened) provider instance for ``provider_id``."""
        key = self.key_for(provider_id)
        async with self._lock:
            provider = self._instances.get(key)
            if provider is None:
                provider = self._create(key)
                self._instances[key] = provider
            if key not in self._opened:
                await provider.open(
                    run_id=self.ctx.run.run_id,
                    workdir=str(self.ctx.workdir),
                    env=self.ctx.env,
                    max_parallel=self.ctx.runtime.max_parallel,
                )
                self._opened.add(key)
            return provider

    async def peek(self, provider_id: str) -> Provider:
        """The provider instance for ``provider_id`` WITHOUT opening it.

        ``open()`` acquires per-run resources — for the real providers a CLI subprocess and a
        worker pool — so read-only callers that only need provider metadata (the toolchain
        probe) must not go through :meth:`get`. The instance is created and cached exactly as
        :meth:`get` would create it, so a later :meth:`get` reuses it and still opens it once.
        """
        key = self.key_for(provider_id)
        async with self._lock:
            provider = self._instances.get(key)
            if provider is None:
                provider = self._create(key)
                self._instances[key] = provider
            return provider

    def _create(self, key: str) -> Provider:
        from rayspec.providers.registry import create_provider

        settings: dict[str, Any] = dict(self.ctx.options.provider_settings.get(key, {}))
        if key == "stub" and self.ctx.options.stub_script is not None:
            settings["script"] = self.ctx.options.stub_script
        return create_provider(key, settings)

    async def aclose(self) -> None:
        """Close every opened provider (errors are swallowed: the run is over)."""
        for key in list(self._opened):
            provider = self._instances.get(key)
            if provider is None:
                continue
            with contextlib.suppress(Exception), anyio.CancelScope(shield=True):
                await provider.aclose()  # best effort: the run is over
        self._opened.clear()


class RunContext:
    """Everything a step needs at run time (see the module docstring)."""

    def __init__(
        self,
        *,
        resolved: ResolvedWorkflow,
        run: RunRecord,
        store: RunStore,
        sinks: EventSink,
        engine: TemplateEngine,
        runtime: Runtime,
        options: RunOptions,
        workdir: Path,
        project: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
        approval_prompt: ApprovalPrompt | None = None,
        providers: Mapping[str, Provider] | None = None,
        cache: Mapping[str, StepRecord] | None = None,
        hash_mismatch: bool = False,
        secret_inputs: Mapping[str, Any] | None = None,
        envelope: Any = None,
    ) -> None:
        self.resolved = resolved
        self.run = run
        self.store = store
        self.sinks = sinks
        self.engine = engine
        self.runtime = runtime
        self.options = options
        self.workdir = Path(workdir)
        self.project: dict[str, Any] = dict(project)
        self.env: dict[str, str] = dict(os.environ if env is None else env)
        self.approval_prompt = approval_prompt
        self.providers = ProviderPool(self, providers)
        self.cache: dict[str, StepRecord] = dict(cache or {})
        self.hash_mismatch = hash_mismatch
        #: the real values of the ``secret: true`` inputs — kept out of every template
        #: context, record and event; only :meth:`secret_env` / :meth:`secret_context` hand them
        #: to shell/python steps (``RAYSPEC_INPUT_<NAME>`` and the step's own ``env:`` mapping)
        self.secret_inputs: dict[str, Any] = dict(secret_inputs or {})
        #: set once a sink raised; later events go to the store only (see :meth:`emit`)
        self.sinks_broken = False
        self.sinks_error: BaseException | None = None
        self.lock = anyio.Lock()
        self.reused_paths: list[str] = []
        #: set when an approval gate recorded a pause (the runner maps it to exit 3)
        self.paused: RunPaused | None = None
        #: set when a ``stop:`` step (or rejected gate) ended the run
        self.stopped: RunStopped | None = None
        self.executors: dict[str, ExecutorFn] = {}
        #: optional pricing table (config) used when a provider reports no USD cost
        self.price_table: Any = None
        #: set (to the reason) once the run-level cap tripped: no new step starts
        self.budget_exceeded: str | None = None
        #: the operator's cross-run spending envelope / circuit breaker
        #: (:class:`rayspec.limits.envelope.RunEnvelope`), or ``None`` when no policy caps this
        #: machine. Unlike the in-workflow caps it PAUSES the run instead of failing it.
        self.envelope: Any = envelope
        #: set (to the reason) once the envelope stopped the run — the final status is ``paused``
        self.envelope_pause: str | None = None
        #: which operational control stopped it: ``budget`` (money) or ``failures`` (the
        #: consecutive-failure breaker). They are separate controls and separate decisions.
        self.envelope_pause_kind: str = "budget"
        #: the record path of the last step that reached a final outcome (what a pause names)
        self.last_finished_path: str | None = None
        #: record paths finished (or replayed) in THIS run — what the caps are measured over;
        #: stale cache records of a resumed run do not count until they are replayed/re-run
        self.accounted_paths: set[str] = set()
        self._run_dir: Path | None = None

    # -- paths ----------------------------------------------------------------------------

    @property
    def run_dir(self) -> Path:
        if self._run_dir is None:
            self._run_dir = Path(self.store.run_dir(self.run.run_id))
        return self._run_dir

    @property
    def tmp_dir(self) -> Path:
        path = self.run_dir / "tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def step_dir(self, path: StepPath | str) -> Path:
        return Path(self.store.step_dir(self.run.run_id, str(path)))

    # -- templating -----------------------------------------------------------------------

    def run_vars(self) -> dict[str, Any]:
        """The ``run.*`` context root."""
        ws = self.run.workspace
        return {
            "id": self.run.run_id,
            "workflow": self.run.workflow_name,
            "workdir": str(self.workdir),
            "artifacts_dir": str(self.run_dir / "artifacts"),
            "state_dir": str(self.run_dir),
            "branch": ws.branch,
            "base_branch": ws.base_branch,
            "started_at": self.run.started_at.isoformat() if self.run.started_at else None,
        }

    def template_context(self, scope: ExecScope) -> dict[str, Any]:
        """The mapping handed to ``TemplateEngine.render_*`` / ``eval_*`` for ``scope``."""
        return build_context(
            scope.tscope,
            inputs=scope.inputs,
            run=self.run_vars(),
            project=self.project,
            env=self.env,
        )

    def secret_context(self, scope: ExecScope) -> dict[str, Any]:
        """:meth:`template_context` with the real secret values in place of the ``<secret>``
        placeholders — ONLY for rendering a shell/python step's ``env:`` mapping.

        Secrets belong to the root workflow: the substitution happens in the root scope only (an
        include body's ``inputs`` are its own ``with:`` bindings and cannot hold a secret —
        such a body still receives every secret through :meth:`secret_env`). The placeholder
        string is reserved: a root input recorded as ``"<secret>"`` is, by construction, a secret.
        """
        inputs = dict(scope.inputs)
        if scope.parent is None:
            inputs.update(self._exported_secrets())
        return build_context(
            scope.tscope,
            inputs=inputs,
            run=self.run_vars(),
            project=self.project,
            env=self.env,
        )

    def secret_env(self, scope: ExecScope) -> dict[str, str]:
        """``RAYSPEC_INPUT_<NAME>`` → real value for every secret input the run was given (the
        process environment of shell/python steps, include bodies included; never persisted).

        Which secrets exist is decided against the run's root inputs (``run.inputs``, redacted
        to ``<secret>``), not against ``scope`` — an include body's scope holds its own inputs,
        so the export is the same in every scope.
        """
        from rayspec.loader.inputs import env_var_name

        del scope  # every scope of the run sees the same secrets
        return {
            env_var_name(name): stringify_scalar(value)
            for name, value in self._exported_secrets().items()
        }

    def _exported_secrets(self) -> dict[str, Any]:
        """Name → real value for every secret recorded as ``<secret>`` in the run's root inputs
        and supplied to this process (given at launch, or re-supplied on resume)."""
        from rayspec.loader.inputs import SECRET_PLACEHOLDER

        return {
            name: value
            for name, value in self.secret_inputs.items()
            if self.run.inputs.get(name) == SECRET_PLACEHOLDER
        }

    def render_env(self, env: Mapping[str, Any], tctx: Mapping[str, Any]) -> dict[str, str]:
        """Deep-render ``env:`` values and str-coerce them (``None`` is an error)."""
        out: dict[str, str] = {}
        for key, raw in env.items():
            value = self.engine.render_value(raw, tctx)
            out[str(key)] = stringify_text(value)
        return out

    def timeout_for(self, step: StepModel, scope: ExecScope) -> float | None:
        """Per-attempt timeout: the step's own, else the scope's ``defaults.timeout``."""
        if step.timeout is not None:
            return float(step.timeout)
        if scope.defaults.timeout:
            return float(scope.defaults.timeout)
        return None

    # -- records / persistence ------------------------------------------------------------

    def new_record(self, step: StepModel, scope: ExecScope) -> StepRecord:
        """A fresh ``running`` record for ``step`` in ``scope``.

        On resume the previous record's ``attempts`` continue and its ``usage`` / ``cost_usd`` /
        ``cost_source`` / ``usage_unknown`` carry over: usage and cost are summed over
        every attempt of a step, whichever run made them — an interrupted attempt's tokens stay
        in the totals after the resume.
        """
        path = scope.record_path(step.id)
        prev = self.cache.get(str(path))
        return StepRecord(
            path=str(path),
            id=step.id,
            kind=type(step).kind,
            status=StepStatus.RUNNING,
            attempts=prev.attempts if prev else 0,
            started_at=utcnow(),
            iteration=scope.iteration,
            item_index=scope.item_index,
            item_sha256=scope.item_sha256,
            usage=prev.usage if prev else Usage(),
            cost_usd=prev.cost_usd if prev else None,
            cost_source=prev.cost_source if prev and prev.cost_usd is not None else "none",
            usage_unknown=prev.usage_unknown if prev else False,
        )

    def _note_progress(self, record: StepRecord) -> None:
        """Remember where the run got to — what an envelope pause names as its location.

        Frozen once the envelope tripped, so the pause names the step that reached the ceiling
        and not the last step the drain skipped afterwards.
        """
        if self.envelope_pause is None:
            self.last_finished_path = record.path

    async def save_record(self, record: StepRecord) -> None:
        """Store a (non-final) record and save ``run.json``."""
        async with self.lock:
            self.run.steps[record.path] = record
            self._note_progress(record)
            await to_thread.run_sync(self.store.save, self.run)

    async def persist(self, outcome: StepOutcome) -> None:
        """Write-ahead: output file (when there is an output) → record → ``run.json``."""
        record = outcome.record
        async with self.lock:
            if outcome.output is not None and not outcome.reused:
                kind = outcome.output_kind or (
                    "text" if isinstance(outcome.output, str) else "json"
                )
                content = (
                    outcome.output
                    if kind == "text" and isinstance(outcome.output, str)
                    else json.dumps(outcome.output, ensure_ascii=False, indent=2) + "\n"
                )
                await to_thread.run_sync(self._write_output, record, content, kind)
                outcome.output_kind = kind
            self.run.steps[record.path] = record
            self._note_progress(record)
            await to_thread.run_sync(self.store.save, self.run)

    def _write_output(self, record: StepRecord, content: str, kind: str) -> None:
        """Write the output file (fsync) and stamp ``output_ref/kind/sha256`` on the record."""
        written: Any = getattr(self.store, "write_output_with_sha", None)
        if callable(written):
            info: Any = written(self.run.run_id, record.path, content, kind=kind)
            record.output_ref = info.output_ref
            record.output_kind = info.kind
            record.output_sha256 = info.sha256
        else:
            record.output_ref = self.store.write_output(
                self.run.run_id, record.path, content, kind=kind
            )
            record.output_kind = kind
            record.output_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def write_artifacts(self, record: StepRecord, files: Sequence[tuple[str, Path]]) -> None:
        """Copy the step's declared artifacts into the run dir and stamp ``record.artifacts``.

        ``files`` is ``(declared path, absolute path)`` per artifact, already checked by
        :mod:`rayspec.engine.executors.artifacts`. The copies go through the store (redacted,
        ``0600``, atomic) in a worker thread under the persistence lock, like every other file
        the run writes. A store that cannot keep a copy is not a step failure: the promise was
        kept, so the artifact is recorded without a ``ref`` and a warning says why.
        """
        if not files:
            return
        async with self.lock:
            warnings = await to_thread.run_sync(self._write_artifacts, record, list(files))
        for message in warnings:
            await self.warn(message)

    def _write_artifacts(self, record: StepRecord, files: list[tuple[str, Path]]) -> list[str]:
        """Store each artifact; returns the warnings the caller must emit."""
        writer: Any = getattr(self.store, "write_artifact", None)
        refs: list[ArtifactRef] = []
        warnings: list[str] = []
        for declared, path in files:
            if callable(writer):
                try:
                    info: Any = writer(self.run.run_id, record.path, declared, path)
                except OSError as exc:
                    warnings.append(
                        f"could not keep a copy of artifact {declared!r} of step "
                        f"{record.path}: {exc}"
                    )
                else:
                    refs.append(
                        ArtifactRef(
                            path=declared,
                            ref=info.artifact_ref,
                            sha256=info.sha256,
                            size=info.size,
                        )
                    )
                    continue
            digest, size = _digest_of(path, self.store.redactor)
            refs.append(ArtifactRef(path=declared, ref=None, sha256=digest, size=size))
        record.artifacts = refs
        return warnings

    async def save_run(self) -> None:
        """Save ``run.json`` (worker thread, under the persistence lock)."""
        async with self.lock:
            await to_thread.run_sync(self.store.save, self.run)

    def save_run_sync(self) -> None:
        """Synchronous flush (used by the second-SIGINT hard-exit path)."""
        self.store.save(self.run)

    # -- events ---------------------------------------------------------------------------

    async def emit(
        self, type_: EventType, *, step_path: str | None = None, **data: Any
    ) -> RunEvent:
        """Append a lifecycle event to the store and fan it out to the sinks."""
        event = RunEvent(type=type_, run_id=self.run.run_id, step_path=step_path, data=data)
        self.store.append_event(self.run.run_id, event)
        if not self.sinks_broken:
            try:
                await self.sinks.emit(event)
            except (Exception, SystemExit) as exc:  # observers never fail a run
                self._drop_sinks(exc)
        return event

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        """Append a stream record (``steps/<path>/stream.jsonl``) and notify the sinks."""
        self.store.append_stream(self.run.run_id, step_path, record)
        if not self.sinks_broken:
            try:
                await self.sinks.emit_stream(step_path, record)
            except (Exception, SystemExit) as exc:  # observers never fail a run
                self._drop_sinks(exc)

    def _drop_sinks(self, exc: BaseException) -> None:
        """A sink raised (typically ``BrokenPipeError`` / Rich's ``SystemExit`` when stdout was
        closed by ``| head``): stop observing, keep running — the store still has everything."""
        self.sinks_broken = True
        self.sinks_error = exc
        log.warning(
            "event sink failed (%s: %s); console output stops, the run continues",
            type(exc).__name__,
            exc,
        )

    async def warn(self, message: str, *, step_path: str | None = None) -> None:
        await self.emit(EventType.WARNING, step_path=step_path, message=message)

    # -- drain policy ---------------------------------------------------------------------

    def keep_going_for(self, scope: ExecScope) -> bool:
        """Whether a failed step leaves independent branches of ``scope`` running.

        ``defaults.on_step_failure: continue`` of the workflow that owns this sibling list (see
        :func:`effective_on_step_failure`).

        Only relaxes draining caused by a *failure*: a pause or a stop still halts new work, and
        the failed step's own dependents still skip with ``upstream_failed`` — ``continue`` is not
        ``allow_failure``. ``--fail-fast`` beats it, because the flag may only ever tighten.
        """
        if self.options.fail_fast:
            return False
        return scope.on_step_failure == "continue"

    def fail_fast_for(self, scope: ExecScope) -> bool:
        """Whether a failed step cancels the running siblings of ``scope``.

        ``--fail-fast`` (``RunOptions.fail_fast``) OR the ``defaults.on_step_failure: fail_fast``
        in force for this sibling list. The CLI flag can only *enable* fail-fast: it never
        downgrades a workflow that asked for it, and ``drain`` (the default) is the 1.0.0
        behaviour. The scheduler reads this, never ``options.fail_fast``.
        """
        if self.options.fail_fast:
            return True
        return scope.on_step_failure == "fail_fast"

    # -- run-level circuit breaker --------------------------------------------------------

    @property
    def caps_set(self) -> bool:
        """Whether the root workflow sets any run-level cap (``budget_usd`` / ``max_tokens`` /
        ``timeout_total``)."""
        defaults = self.resolved.workflow.defaults
        return (
            defaults.budget_usd is not None
            or defaults.max_tokens is not None
            or defaults.timeout_total is not None
        )

    @property
    def time_capped(self) -> bool:
        """Whether the root workflow sets ``defaults.timeout_total`` (the wall-clock cap)."""
        return self.resolved.workflow.defaults.timeout_total is not None

    def elapsed_s(self) -> float | None:
        """Wall-clock seconds since the run's ORIGINAL start, or ``None`` before it started.

        ``RunRecord.started_at`` is stamped once and kept by every resume entry, so a run with
        ``timeout_total: 2h`` gets two hours of run — not two hours per attempt. Time spent
        waiting at an approval gate is part of it.
        """
        started = self.run.started_at
        if started is None:
            return None
        return max(0.0, (utcnow() - started).total_seconds())

    def budget_totals(self, pending: StepRecord | None = None) -> tuple[Usage, float | None, str]:
        """``(usage, cost_usd, cost_source)`` over the records accounted in this run (finished or
        replayed) plus ``pending`` (an in-flight leaf between attempts); ``cost_source`` per
        :func:`cost_source_of`."""
        records = [r for p, r in self.run.steps.items() if p in self.accounted_paths]
        if pending is not None and pending.path not in self.accounted_paths:
            records.append(pending)
        return totals_of(records)

    def run_totals(self) -> tuple[Usage, float | None, str]:
        """``(usage, cost_usd, cost_source)`` over EVERY record of the run (what ``run.json``,
        the ``run.finished`` event and the approval panel report)."""
        return totals_of(self.run.steps.values())

    async def check_envelope(self) -> str | None:
        """Commit this run's spend to the local ledger and ask whether it may go on.

        The operator's ceilings (``policy.budget``, ``policy.max_consecutive_failures``) are a
        different instrument from the workflow's own ``defaults.budget_usd``: reaching one is
        not a defect, it is the moment the machine was supposed to stop and ask. So the run
        DRAINS like any capped run (``budget_exceeded``: nothing new starts, running steps
        finish) but ends ``paused`` (exit 3) rather than failed, and ``rayspec approve`` /
        ``rayspec resume`` continue it.

        A dry run never touches the ledger: it spends nothing.
        """
        envelope = self.envelope
        if envelope is None or self.options.dry_run or not envelope.active:
            return None
        _usage, cost, _source = self.run_totals()
        reason = await to_thread.run_sync(envelope.check, cost)
        for problem in envelope.take_warnings():
            await self.warn(problem)
        if reason is None:
            return None
        self.envelope_pause = reason
        self.envelope_pause_kind = envelope.pause_kind
        self.budget_exceeded = reason  # drain: no new step starts, running ones finish
        await self.warn(
            f"{reason}: the run pauses — resume it with `rayspec resume {self.run.run_id}` "
            f"once the ceiling allows, or `rayspec approve {self.run.run_id}` to continue anyway"
        )
        return reason

    async def check_budget(self, *, pending: StepRecord | None = None) -> str | None:
        """Compare the run totals (:meth:`budget_totals`) and the elapsed wall clock
        (:meth:`elapsed_s`) with the root ``defaults`` caps; the first time one of them is
        exceeded, remember the reason, emit a ``warning`` and return it. Later calls just return
        the remembered reason.

        The cost/token caps and ``timeout_total`` are ONE breaker: whichever trips first sets
        :attr:`budget_exceeded`, and the run drains and ends ``failed`` the same way."""
        if self.budget_exceeded is not None:
            return self.budget_exceeded
        envelope_reason = await self.check_envelope()
        if envelope_reason is not None:
            return envelope_reason
        if not self.caps_set:
            return None
        defaults = self.resolved.workflow.defaults
        usage, cost, source = self.budget_totals(pending)
        breaches = cap_reasons(usage, cost, source, self.elapsed_s(), defaults)
        if not breaches:
            return None
        # every cap that is over is named: one trip, one reason, no silent loser
        reason = "; ".join(breach.reason for breach in breaches)
        knobs = " / ".join(dict.fromkeys(knob for breach in breaches for knob in breach.knobs))
        self.budget_exceeded = reason
        await self.warn(
            f"{reason}: no new steps start, running steps finish; the run ends failed — "
            f"raise {knobs} and resume (--force: the workflow hash changes)"
        )
        return reason

    # -- resume cache ---------------------------------------------------------------------

    def read_output_value(self, record: StepRecord) -> Any:
        """Load a stored output (text or parsed JSON); raises ``FileNotFoundError``."""
        if record.output_ref is None:
            raise FileNotFoundError(record.path)
        text = self.store.read_output(self.run.run_id, record.output_ref)
        if record.output_kind == "json":
            return json.loads(text)
        return text


# --------------------------------------------------------------------------------------------------
# helpers shared by scheduler/executors
# --------------------------------------------------------------------------------------------------


def _digest_of(path: Path, redactor: Redactor) -> tuple[str, int]:
    """``(sha256, size)`` of a file as the store would KEEP it, read in chunks.

    Used when the store keeps no copy: the record still describes what the step produced. The
    bytes are streamed through ``redactor`` exactly as :meth:`FileRunStore.write_artifact` does,
    so ``sha256``/``size`` mean the same thing whichever store is installed — and a file that
    carried a ``secret: true`` value leaves no digest of the secret behind either.
    """
    digest = hashlib.sha256()
    size = 0
    stream = redactor.stream() if redactor else None

    def feed(text: str) -> None:
        nonlocal size
        data = text.encode("utf-8", "surrogateescape")
        digest.update(data)
        size += len(data)

    with open(path, "rb") as fh:
        while chunk := fh.read(_ARTIFACT_CHUNK_BYTES):
            text = chunk.decode("utf-8", "surrogateescape")
            feed(stream.feed(text) if stream is not None else text)
        if stream is not None:
            feed(stream.flush())
    return digest.hexdigest(), size


def error_info(
    exc: BaseException, *, transient: bool = False, type_: str | None = None
) -> ErrorInfo:
    """An :class:`ErrorInfo` from an exception (``hint`` appended when present)."""
    message = str(exc) or type(exc).__name__
    hint = getattr(exc, "hint", None)
    if hint and hint not in message:
        message = f"{message} (fix: {hint})"
    return ErrorInfo(type=type_ or type(exc).__name__, message=message, transient=transient)


def body_ids(step: StepModel) -> frozenset[str]:
    if isinstance(step, LoopStep):
        return frozenset(s.id for s in step.loop.steps)
    if isinstance(step, EachStep):
        return frozenset(s.id for s in step.steps)
    return frozenset()


def view_of(outcome: StepOutcome, step: StepModel | None = None) -> StepView:
    """The :class:`StepView` templates see for a finished (or replayed) step."""
    rec = outcome.record
    return StepView(
        id=rec.id,
        kind=rec.kind,
        status=rec.status,
        output=outcome.output,
        ok=rec.ok,
        exit_code=rec.exit_code,
        stderr=outcome.stderr,
        duration_s=(rec.duration_ms / 1000.0) if rec.duration_ms is not None else None,
        cost_usd=rec.cost_usd,
        usage=rec.usage if (rec.usage.total or rec.kind == "prompt") else None,
        session=rec.session_ref.id if rec.session_ref is not None else None,
        model=rec.model,
        approved=rec.approved,
        iterations=rec.loop.iterations if rec.loop is not None else None,
        converged=rec.loop.converged if rec.loop is not None else None,
        items=outcome.items,
        skip_reason=rec.skip_reason,
        error=rec.error.message if rec.error is not None else None,
        tolerated=rec.tolerated,
        denials=[d.model_dump() for d in rec.denials] if rec.kind == "prompt" else None,
        body_ids=body_ids(step) if step is not None else frozenset(),
    )


def usage_from_mapping(raw: Mapping[str, Any]) -> Usage:
    """:class:`Usage` from a ``{input, cached_input, cache_write, output, reasoning}`` mapping
    (missing / non-numeric counters read as 0)."""

    def num(key: str) -> int:
        value = raw.get(key)
        return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0

    return Usage(
        input=num("input"),
        cached_input=num("cached_input"),
        cache_write=num("cache_write"),
        output=num("output"),
        reasoning=num("reasoning"),
    )


def merge_cost_source(previous: str, new: str) -> str:
    """Cost source of a sum: ``table`` once any addend is an estimate, ``provider`` when every
    addend is provider-reported, the other one when one side is ``none``."""
    if previous == "none":
        return new
    if new == "none":
        return previous
    return "table" if "table" in (previous, new) else "provider"


def sha256_json(value: Any) -> str:
    """Stable sha256 of a JSON-able value (``item_sha256``, fingerprints)."""
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


__all__ = [
    "BUDGET_SKIP_REASON",
    "CAP_KNOBS",
    "CAP_REASON_PREFIXES",
    "LEAF_KINDS",
    "ON_STEP_FAILURE_ORDER",
    "REUSABLE_KINDS",
    "CapBreach",
    "ExecScope",
    "ExecutorFn",
    "ProviderPool",
    "RunContext",
    "RunOptions",
    "StepOutcome",
    "body_ids",
    "budget_parts",
    "budget_reason",
    "cap_reasons",
    "cost_source_of",
    "effective_on_step_failure",
    "error_info",
    "failed_outcome",
    "is_cap_reason",
    "mark_failed",
    "merge_cost_source",
    "on_step_failure_floor",
    "sha256_json",
    "stated_on_step_failure",
    "strictest_on_step_failure",
    "time_reason",
    "totals_of",
    "usage_from_mapping",
    "utcnow",
    "view_of",
]
