# SPDX-License-Identifier: Apache-2.0
"""``prompt:`` executor — the only step kind that calls a provider.

Module boundary: resolves the step's agent (loader) → builds the neutral
:class:`~rayspec.providers.base.AgentRequest` → streams ``AgentEvent`` s to
``steps/<path>/stream.jsonl`` + the sinks → maps the :class:`AgentResult` onto the record.
Structured output goes through :mod:`rayspec.engine.structured`; retries/timeouts are the
scheduler's leaf loop; ``session: <id>`` continues the referenced step's recorded session.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from anyio import to_thread

from rayspec.engine.context import (
    BUDGET_SKIP_REASON,
    ExecScope,
    RunContext,
    StepOutcome,
    error_info,
    failed_outcome,
    sha256_json,
    usage_from_mapping,
)
from rayspec.engine.retry import TIMEOUT_ERROR_TYPE, classify_agent_error, classify_provider_error
from rayspec.engine.structured import run_structured
from rayspec.events.model import StreamRecord
from rayspec.loader import ResolvedAgent
from rayspec.providers.base import (
    AccessLevel,
    AgentEvent,
    AgentRequest,
    AgentResult,
    EmitFn,
    McpServerSpec,
    Provider,
    ProviderError,
    ToolPolicy,
    Usage,
)
from rayspec.schema import PromptStep, StepModel, StepStatus
from rayspec.store.model import DenialInfo, ErrorInfo, SessionRef, StepRecord
from rayspec.templating import TemplateRenderError


def agent_fingerprint_data(agent: ResolvedAgent) -> dict[str, Any]:
    """The agent fields a step's fingerprint hashes: everything that changes what the provider
    is ASKED.

    ``on_denial`` is deliberately absent. It changes how rayspec *grades* a turn, not what the
    turn is, and including it would make flipping ``warn`` → ``fail`` re-run every finished
    prompt step of a resumed run to re-grade a record that already carries its ``denials``.
    """
    return {
        "provider": agent.provider,
        "model": agent.model,
        "effort": agent.effort,
        "access": agent.access,
        "instructions_mode": agent.instructions_mode,
        "max_turns": agent.max_turns,
        "budget_usd": agent.budget_usd,
        "tools": {"allow": list(agent.tools.allow), "deny": list(agent.tools.deny)},
        "thinking": agent.thinking,
        "mcp": {k: v.model_dump() for k, v in agent.mcp.items()},
        "provider_options": agent.provider_options,
    }


def prompt_fingerprint(step: PromptStep, scope: ExecScope, ctx: RunContext) -> str:
    """sha256 over the rendered prompt + instructions + resolved agent + schema."""
    def_path = scope.def_path(step.id)
    agent = ctx.resolved.agent_for(def_path)
    tctx = ctx.template_context(scope)
    prompt = ctx.engine.render_str(ctx.resolved.prompt_text(def_path) or "", tctx)
    instructions = (
        ctx.engine.render_str(agent.instructions, tctx) if agent.instructions is not None else None
    )
    return sha256_json(
        {
            "prompt": prompt,
            "instructions": instructions,
            "agent": agent_fingerprint_data(agent),
            "schema": step.output_schema,
            "session": step.session,
        }
    )


def make_emit(ctx: RunContext, path: str, attempt: int) -> EmitFn:
    """An ``EmitFn`` recording every agent event to the store + sinks."""

    async def emit(event: AgentEvent) -> None:
        await ctx.emit_stream(path, StreamRecord.from_agent_event(event, attempt=attempt))

    return emit


class UsageTracker:
    """An ``EmitFn`` wrapper that remembers the usage the adapter reported so far.

    Adapters stream ``usage`` events whose ``data["turn_total"]`` is the cumulative usage of the
    attempt (Codex: per ``thread/tokenUsage`` update; Claude: per completed assistant message);
    without ``turn_total`` the ``data["usage"]`` deltas are summed. When the attempt is
    interrupted, times out or ends ``interrupted`` without a result usage, :attr:`partial` is
    what the step records; ``None`` means nothing was reported — usage unknown, not zero.
    """

    def __init__(self, inner: EmitFn) -> None:
        self.inner = inner
        self.partial: Usage | None = None

    async def __call__(self, event: AgentEvent) -> None:
        if event.kind == "usage":
            total = event.data.get("turn_total")
            delta = event.data.get("usage")
            if isinstance(total, Mapping):
                self.partial = usage_from_mapping(total)
            elif isinstance(delta, Mapping):
                self.partial = (self.partial or Usage()) + usage_from_mapping(delta)
        await self.inner(event)


def session_for(
    step: PromptStep, scope: ExecScope, ctx: RunContext, provider_id: str
) -> tuple[str | None, str | None]:
    """``(session id to resume, warning)`` for ``session: <id>`` (ancestor or self in a loop)."""
    target = step.session
    if target is None:
        return None, None
    ref: SessionRef | None = None
    if target == step.id:
        # self inside a loop: the previous iteration's record
        prefix = scope.prefix
        if prefix.is_root or prefix.index is None or prefix.index <= 1:
            return None, None
        prev_path = prefix.parent.child(prefix.leaf_id).indexed(prefix.index - 1).child(step.id)
        rec = ctx.run.steps.get(str(prev_path))
        ref = rec.session_ref if rec is not None else None
    else:
        walk: ExecScope | None = scope
        while walk is not None:
            if target in walk.views:
                rec = ctx.run.steps.get(str(walk.record_path(target)))
                if rec is not None:
                    ref = rec.session_ref
                break
            walk = walk.parent
        if ref is None:
            view = scope.tscope.lookup_step(target)
            if view is not None and isinstance(view.session, str):
                return view.session, None
    if ref is None:
        return None, None
    if ref.provider != provider_id:
        return None, (
            f"session: {target} was recorded with provider {ref.provider!r}, "
            f"this step runs on {provider_id!r}; starting a fresh session"
        )
    return ref.id, None


def build_request(
    step: PromptStep,
    agent: ResolvedAgent,
    provider: Provider,
    *,
    path: str,
    prompt: str,
    instructions: str | None,
    env: Mapping[str, str],
    cwd: str,
    resume_session: str | None,
    timeout_s: float | None,
    run_id: str,
    attempt: int,
) -> AgentRequest:
    """The neutral request for one attempt (``output_schema`` is set by structured.py)."""
    mcp = tuple(
        McpServerSpec(
            name=name,
            transport=spec.transport,
            command=spec.command,
            args=tuple(spec.args),
            env=dict(spec.env),
            url=spec.url,
            headers=dict(spec.headers),
        )
        for name, spec in agent.mcp.items()
    )
    return AgentRequest(
        step_path=path,
        prompt=prompt,
        cwd=cwd,
        access=AccessLevel(agent.access),
        instructions=instructions,
        instructions_mode=agent.instructions_mode,  # type: ignore[arg-type]
        model=agent.model,
        effort=agent.effort,
        tools=ToolPolicy(allow=tuple(agent.tools.allow), deny=tuple(agent.tools.deny)),
        env=dict(env),
        max_turns=agent.max_turns,
        budget_usd=agent.budget_usd,
        thinking=agent.thinking,
        output_schema=None,
        resume_session=resume_session,
        mcp_servers=mcp,
        timeout_s=timeout_s,
        provider_options=dict(agent.provider_options.get(provider.id, {})),
        run_id=run_id,
        step_attempt=attempt,
    )


async def run_prompt(
    step: StepModel, scope: ExecScope, ctx: RunContext, record: StepRecord, attempt: int
) -> StepOutcome:
    """One attempt of a ``prompt:`` step."""
    assert isinstance(step, PromptStep)
    if ctx.envelope_pause is not None and not ctx.options.dry_run:
        # The operator's ceiling has been reached. A `join: always` step is exempt from the
        # drain gate — a cleanup step must still run when a run is stopping — but "run the
        # cleanup" is not "spend more money": no further provider turn is opened, whatever the
        # step's join policy is. A ceiling a workflow can opt out of with four characters of
        # YAML is not a ceiling.
        await ctx.warn(f"{ctx.envelope_pause}: no further agent turn", step_path=record.path)
        record.status = StepStatus.SKIPPED
        record.skip_reason = BUDGET_SKIP_REASON
        record.ok = None
        return StepOutcome(record=record)
    def_path = scope.def_path(step.id)
    try:
        agent = ctx.resolved.agent_for(def_path)
    except KeyError:
        return failed_outcome(
            record, ErrorInfo(type="agent", message=f"no agent resolved for {def_path}")
        )
    record.provider = agent.provider
    record.model = agent.model
    try:
        provider = await ctx.providers.get(agent.provider)
    except ProviderError as exc:
        return failed_outcome(record, classify_provider_error(exc))
    tctx = ctx.template_context(scope)
    prompt_text = ctx.resolved.prompt_text(def_path)
    if prompt_text is None:
        return failed_outcome(record, ErrorInfo(type="prompt", message="prompt text not loaded"))
    try:
        prompt = ctx.engine.render_str(prompt_text, tctx)
        instructions = (
            ctx.engine.render_str(agent.instructions, tctx)
            if agent.instructions is not None
            else None
        )
        env = ctx.render_env(step.env, tctx)
    except TemplateRenderError as exc:
        return failed_outcome(record, error_info(exc, type_="render"))
    record.fingerprint = sha256_json(
        {
            "prompt": prompt,
            "instructions": instructions,
            "agent": agent_fingerprint_data(agent),
            "schema": step.output_schema,
            "session": step.session,
        }
    )
    await persist_prompt(ctx, record, prompt)
    resume_session, warning = session_for(step, scope, ctx, provider.id)
    if warning:
        await ctx.warn(warning, step_path=record.path)
    req = build_request(
        step,
        agent,
        provider,
        path=record.path,
        prompt=prompt,
        instructions=instructions,
        env=env,
        cwd=str(ctx.workdir),
        resume_session=resume_session,
        timeout_s=ctx.timeout_for(step, scope),
        run_id=ctx.run.run_id,
        attempt=attempt,
    )
    emit = UsageTracker(make_emit(ctx, record.path, attempt))
    structured_value: Any = None
    structured_error: str | None = None
    # PRD-07 R2: a heartbeat right before and after the call, on top of the periodic timer — a
    # single long agent turn proves it is still alive at both ends, not only once every interval
    await ctx.touch_heartbeat()
    try:
        if step.output_schema is not None:
            sres = await run_structured(provider, req, emit, step.output_schema)
            result = sres.result
            structured_value = sres.value
            structured_error = sres.error
            usage, cost = sres.usage, sres.cost_usd
        else:
            result = await provider.run(req, emit)
            usage, cost = result.usage, result.cost_usd
        await ctx.touch_heartbeat()
    except ProviderError as exc:
        # a raised provider error (auth, CLI missing, a 429 the SDK raised): keep what the
        # stream reported before it, else zero — NOT "unknown", which is reserved for attempts
        # cut off mid-flight
        _record_partial_usage(record, ctx, emit, model=agent.model, unknown_if_unreported=False)
        return failed_outcome(record, classify_provider_error(exc))
    except BaseException:
        # cancelled from outside (Ctrl-C, a sibling's stop/pause, the attempt deadline): keep the
        # usage the adapter reported so far, or mark it unknown — never "zero tokens"
        _record_partial_usage(record, ctx, emit, model=agent.model)
        raise
    if result.status in {"timeout", "interrupted"} and not usage.total and cost is None:
        # the adapter cut the attempt short without a usage total: fall back to the stream
        _record_partial_usage(record, ctx, emit, model=result.model or agent.model)
    else:
        record.usage = usage
        record.cost_usd = cost
        record.cost_source = result.cost_source
        _estimate_cost(record, ctx, model=result.model, usage=usage)
    record.model = result.model or agent.model
    record.provider = provider.id
    record.denials = [
        DenialInfo(tool=d.tool, reason=d.reason, call_id=d.call_id) for d in result.denials
    ]
    if result.session_ref:
        record.session_ref = SessionRef(provider=provider.id, id=result.session_ref)
    if record.denials:
        await ctx.warn(denial_warning(record), step_path=record.path)
    return _map_result(
        step, record, result, structured_value, structured_error, on_denial=agent.on_denial
    )


async def persist_prompt(ctx: RunContext, record: StepRecord, prompt: str) -> None:
    """Write-ahead: persist the rendered ``prompt`` and stamp ``record.prompt_ref``.

    Goes through ``FileRunStore.write_prompt`` (new store writes never open a file under the
    run dir directly) in a worker thread, like every other fsync-backed store call.
    A store without the method (an older or in-memory :class:`~rayspec.store.base.RunStore`)
    simply has no prompt copy, and a write that fails (full disk, read-only store) leaves
    ``prompt_ref`` unset: losing the debugging copy must never fail the step that is about to
    run. A *failure* is warned about on the recorded channel (``ctx.warn`` → events.jsonl and
    the console), never only in a logger nothing configures — otherwise ``rayspec explain``
    quietly shows a re-render where the contract promises the persisted bytes.
    """
    write = getattr(ctx.store, "write_prompt", None)
    if not callable(write):
        return
    try:
        ref = await to_thread.run_sync(write, ctx.run.run_id, record.path, prompt)
    except (OSError, ValueError) as exc:  # best effort: the prompt copy is a debugging aid
        await ctx.warn(
            f"could not persist the rendered prompt of {record.path}: {exc} "
            "(rayspec explain will re-render it instead of replaying it)",
            step_path=record.path,
        )
        return
    record.prompt_ref = str(ref)


def denial_warning(record: StepRecord) -> str:
    """``2 tool call(s) denied: Bash, Write`` — what the console and events.jsonl show."""
    return f"{len(record.denials)} tool call(s) denied: {denied_tools(record)}"


def denied_tools(record: StepRecord) -> str:
    """The distinct tool names of ``record.denials``, in the order they were refused."""
    return ", ".join(dict.fromkeys(d.tool for d in record.denials))


def _map_result(
    step: PromptStep,
    record: StepRecord,
    result: AgentResult,
    structured_value: Any,
    structured_error: str | None,
    *,
    on_denial: str = "warn",
) -> StepOutcome:
    if result.status == "success" and record.denials and on_denial == "fail":
        # the turn "succeeded", but the agent was refused something it tried to do. With
        # ``on_denial: fail`` that is the step's answer — a denial nobody reads is a silent
        # failure. The denials stay on the record either way.
        return failed_outcome(
            record,
            ErrorInfo(
                type="denied",
                message=(
                    f"the agent was denied {len(record.denials)} tool call(s): "
                    f"{denied_tools(record)}"
                ),
                transient=False,
            ),
            output=result.text or None,
        )
    if result.status == "success":
        if step.output_schema is not None:
            if structured_error is not None:
                return failed_outcome(
                    record,
                    ErrorInfo(
                        type="output_schema",
                        message=f"structured output invalid: {structured_error}",
                        transient=False,
                    ),
                )
            record.status = StepStatus.SUCCEEDED
            record.ok = True
            return StepOutcome(record=record, output=structured_value, output_kind="json")
        record.status = StepStatus.SUCCEEDED
        record.ok = True
        return StepOutcome(record=record, output=result.text, output_kind="text")
    if result.error is not None:
        error = classify_agent_error(result.error)
        if result.status == "timeout":
            error = ErrorInfo(type=TIMEOUT_ERROR_TYPE, message=error.message, transient=False)
    elif result.status == "timeout":
        error = ErrorInfo(type=TIMEOUT_ERROR_TYPE, message="provider timed out", transient=False)
    else:
        error = ErrorInfo(
            type=str(result.status),
            message=_status_message(result),
            transient=False,
        )
    return failed_outcome(record, error, output=result.text or None)


def _estimate_cost(record: StepRecord, ctx: RunContext, *, model: str | None, usage: Usage) -> None:
    """Price ``usage`` from the config pricing table when the adapter reported no cost."""
    if record.cost_usd is not None or ctx.price_table is None or not model:
        return
    estimated = ctx.price_table.cost_usd(model, usage)
    if estimated is not None:
        record.cost_usd = estimated
        record.cost_source = "table"


def _record_partial_usage(
    record: StepRecord,
    ctx: RunContext,
    tracker: UsageTracker,
    *,
    model: str | None,
    unknown_if_unreported: bool = True,
) -> None:
    """Stamp the attempt's partial usage (what the stream reported) on ``record``, priced from
    the table when possible; no report at all ⇒ ``usage_unknown`` — unless
    ``unknown_if_unreported`` is off (a raised ``ProviderError``: zero, not unknown)."""
    partial = tracker.partial
    if partial is None:
        record.usage = Usage()
        record.cost_usd = None
        record.cost_source = "none"
        if unknown_if_unreported:
            record.usage_unknown = True
        return
    record.usage = partial
    record.cost_usd = None
    record.cost_source = "none"
    _estimate_cost(record, ctx, model=model, usage=partial)


def _status_message(result: AgentResult) -> str:
    return {
        "interrupted": "the agent run was interrupted",
        "max_turns": "the agent hit max_turns",
        "budget": "the agent exceeded budget_usd",
        "error": "the agent run failed",
    }.get(result.status, f"agent status {result.status}")


__all__ = [
    "UsageTracker",
    "agent_fingerprint_data",
    "build_request",
    "denial_warning",
    "denied_tools",
    "make_emit",
    "persist_prompt",
    "prompt_fingerprint",
    "run_prompt",
    "session_for",
]
