# SPDX-License-Identifier: Apache-2.0
"""OpenAI Codex SDK adapter: ``provider: codex`` → :class:`CodexProvider`.

Boundary: the only rayspec module that imports ``openai_codex``. It speaks the neutral contract of
:mod:`rayspec.providers.base` (``AgentRequest`` → ``AgentEvent`` stream → ``AgentResult``) and is
loaded lazily by the registry, so validation and ``rayspec plan`` never touch the SDK.

Design (plan §3.1):

* **Client pool** — one ``AsyncCodex`` (= one ``codex app-server`` process) per distinct
  environment (run env + ``AgentRequest.env``), created lazily, closed in :meth:`aclose`. A client
  whose transport died (``TransportClosedError``) is *poisoned*: closed and recreated on next use.
* **Worker budget** — the SDK offloads every blocking call (``asyncio.to_thread``) onto the event
  loop's default executor and an open turn stream pins one worker while it waits. ``open()``
  raises the default executor to ``max(32, 2 * max_parallel + 8)`` workers when it is smaller and
  wraps turns in a :class:`anyio.CapacityLimiter` of ``workers - 4`` so ``interrupt()``,
  ``turn()`` and ``close()`` always find a free worker.
* **Shielded driver** — ``thread_start``/``thread_resume``, ``thread.turn()`` and the
  ``handle.stream()`` loop all run in one child task under ``CancelScope(shield=True)``:
  cancellation must never reach an ``asyncio.to_thread`` call or the generator (it would leak a
  worker thread blocked in ``queue.get`` / leave the turn queue registered). The parent waits on
  an ``Event`` under ``anyio.fail_after(timeout_s)`` — the deadline therefore covers thread start,
  turn start *and* the stream. On timeout/cancel/sink failure it calls ``interrupt()`` (shielded),
  drains for ``drain_s``, and — if the driver is still hung (in ``thread_start`` or the stream) —
  closes the client (waking the worker through ``MessageRouter.fail_all``) and marks it for
  recreation. A turn that completes *successfully* during the drain is still a success
  (``raw.deadline_exceeded = True`` + a warning event). ``handle.stream()`` is consumed under
  ``contextlib.aclosing`` so the turn queue is unregistered deterministically.
* **Usage** — ``thread/tokenUsage/updated`` carries cumulative ``total`` per thread; usage for a
  turn is the delta of ``total`` against the last total seen for that thread (never a sum of
  ``last``). ``AgentResult.raw["usage_total"]`` reports the last cumulative total so the engine
  can pass it back as ``provider_options.codex.usage_baseline`` when resuming the thread in a
  later run. Without a baseline, a thread first seen mid-history uses the **inference**
  ``total - last`` of its first update. **Settled 2026-08-21** by a live run against
  ``codex-cli`` 0.147.0 (``tests/providers/test_codex_live.py::
  test_live_codex_resume_usage_inference_matches_server_totals``, ``RAYSPEC_LIVE=1``): a fresh
  provider instance resuming a thread *without* a baseline reported usage exactly equal to the
  delta of the server totals — the app-server does not replay a carry-over update, so the engine
  does **not** need to pass ``usage_baseline`` back. It stays available as an escape hatch (and
  is used by the pooled client within one run). Re-run that test after a Codex SDK bump.
  ``aclose()`` clears the per-thread totals.

Settings (``config.providers.codex`` / registry ``settings``): ``approval_mode``
(``deny_all`` default | ``auto_review``), ``config`` (extra Codex config merged into every
thread), ``codex_bin`` (override the bundled runtime), ``pricing`` (model → price, see
:mod:`rayspec.providers.pricing`), ``drain_s`` (seconds to wait for an interrupted turn to
finish, default 10). Per-request ``provider_options`` (``codex:`` block, or already narrowed)
accept ``approval_mode``, ``config`` (merged over the settings config), ``ephemeral`` and
``usage_baseline`` (cumulative usage counters of the resumed thread, see above).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import anyio
from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    CodexConfig,
    CodexError,
    JsonRpcError,
    Sandbox,
    ServerBusyError,
    TransportClosedError,
    is_retryable_error,
)
from openai_codex import __version__ as _SDK_VERSION
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    CommandExecutionOutputDeltaNotification,
    CommandExecutionThreadItem,
    ErrorNotification,
    FileChangeThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    McpToolCallThreadItem,
    MessagePhase,
    PlanThreadItem,
    ReasoningEffort,
    ReasoningSummaryTextDeltaNotification,
    ReasoningTextDeltaNotification,
    ReasoningThreadItem,
    ThreadTokenUsageUpdatedNotification,
    TokenUsageBreakdown,
    TurnCompletedNotification,
    TurnError,
    TurnPlanUpdatedNotification,
    TurnStartedNotification,
    TurnStatus,
    WebSearchThreadItem,
)
from openai_codex.models import Notification, UnknownNotification
from pydantic import BaseModel

from rayspec import __version__
from rayspec.providers._schema import for_openai_strict
from rayspec.providers._tools import translate_tools
from rayspec.providers.base import (
    AccessLevel,
    AgentError,
    AgentEvent,
    AgentRequest,
    AgentResult,
    Denial,
    EmitFn,
    ErrorKind,
    ProviderCapabilities,
    ProviderError,
    ProviderHealth,
    ProviderNotInstalledError,
    ResultStatus,
    Usage,
)
from rayspec.providers.capabilities import CODEX_CAPABILITIES
from rayspec.providers.pricing import PriceTable

log = logging.getLogger("rayspec.providers.codex")

#: Default seconds to wait for an interrupted turn to deliver ``turn/completed``.
DEFAULT_DRAIN_S = 10.0
#: Minimum default-executor size the provider insists on (plan §3.2).
MIN_EXECUTOR_WORKERS = 32
#: Workers kept free of turn streams for ``interrupt()`` / ``turn()`` / ``close()``.
SPARE_WORKERS = 4
#: Probe prompt used by ``healthcheck(probe=True)``.
PROBE_PROMPT = "Reply with exactly OK"
PROBE_TIMEOUT_S = 120.0

_SANDBOX: Mapping[AccessLevel, Sandbox] = {
    AccessLevel.READ_ONLY: Sandbox.read_only,
    AccessLevel.WORKSPACE_WRITE: Sandbox.workspace_write,
    AccessLevel.FULL: Sandbox.full_access,
}

#: ``provider_options.codex.config`` key paths the adapter computes itself, so a workflow may not
#: set them (they are dropped with a warning). ``config`` is applied over the adapter's own keys,
#: which is how a workflow could re-enable web search that ``tools.deny: [web]`` or
#: ``network: off`` had switched off, raise its own sandbox or swap its own model. The
#: equivalent for the Claude adapter is :data:`rayspec.providers.claude.ADAPTER_OWNED_OPTIONS`.
#: ``mcp_servers`` is deliberately absent: it is MERGED under the request's own servers rather
#: than replacing them, and ``mcp.allow_servers`` is what checks it (at load time).
#: ``providers.codex.config`` in ``config.yaml`` is unaffected — that belongs to the machine
#: owner, not to the workflow.
ADAPTER_OWNED_CONFIG: tuple[tuple[str, ...], ...] = (
    ("model",),
    ("sandbox_mode",),
    ("approval_policy",),
    ("web_search",),
    ("tools", "web_search"),
)

#: ``codexErrorInfo`` code → (neutral error kind, transient). Unknown codes → ("unknown", False).
_ERROR_INFO: Mapping[str, tuple[ErrorKind, bool]] = {
    "serverOverloaded": ("api", True),
    "internalServerError": ("api", True),
    "httpConnectionFailed": ("transport", True),
    "responseStreamConnectionFailed": ("transport", True),
    "responseStreamDisconnected": ("transport", True),
    "responseTooManyFailedAttempts": ("transport", True),
    "unauthorized": ("auth", False),
    "usageLimitExceeded": ("budget", False),
    "sessionBudgetExceeded": ("budget", False),
    "badRequest": ("api", False),
    "contextWindowExceeded": ("model", False),
    "cyberPolicy": ("api", False),
    "sandboxError": ("sandbox", False),
    "threadRollbackFailed": ("api", False),
    "activeTurnNotSteerable": ("api", False),
    "other": ("unknown", False),
}


# --------------------------------------------------------------------------------------------------
# Small helpers (pure)
# --------------------------------------------------------------------------------------------------


def error_info_code(error: TurnError | None) -> str | None:
    """camelCase ``codexErrorInfo`` code of a ``TurnError`` (``"serverOverloaded"``,
    ``"httpConnectionFailed"``…) or ``None``."""
    if error is None or error.codex_error_info is None:
        return None
    root = error.codex_error_info.root
    if isinstance(root, Enum):
        return str(root.value)
    if isinstance(root, BaseModel):
        dumped = root.model_dump(by_alias=True)
        if len(dumped) == 1:
            return next(iter(dumped))
    return None


def sandbox_denial(error: TurnError | None) -> Denial | None:
    """A ``sandboxError`` turn error as a neutral :class:`Denial` (``None`` for anything else).

    Codex reports a refused command as a turn ERROR rather than as a list of denied calls: the
    turn fails, so the step fails anyway. Recording the denial is still worth it — the record
    then names what the sandbox would not let the agent do instead of only that something went
    wrong — but it is not the same instrument as Claude's ``permission_denials``, and the
    ``denial_reporting`` capability says so.
    """
    if error_info_code(error) != "sandboxError":
        return None
    message = (getattr(error, "message", "") or "").strip()
    return Denial(tool="shell", reason=message or "the sandbox refused the command")


def turn_denials(turn: Any, _state: Any = None) -> tuple[Denial, ...]:
    """The denials of one finished turn — from the TURN's own error only.

    Deliberately not ``state.last_error``: that field collects every ``ErrorNotification`` the
    stream reported, retried ones included, and is overwritten by any later error. Folding it
    into a COMPLETED turn would retro-fail a step whose sandbox refusal was retried and
    recovered, which is the opposite of what the step's outcome says.
    """
    denial = sandbox_denial(getattr(turn, "error", None) if turn is not None else None)
    return (denial,) if denial is not None else ()


def classify_turn_error(error: TurnError | None, *, fallback_message: str = "") -> AgentError:
    """Neutral :class:`AgentError` for a failed turn (plan §3.1 transient/fatal table)."""
    code = error_info_code(error)
    kind, transient = _ERROR_INFO.get(code or "", ("unknown", False))
    message = (error.message if error is not None else "") or fallback_message or "turn failed"
    return AgentError(kind=kind, message=message, transient=transient, code=code)


def usage_from_breakdown(b: TokenUsageBreakdown) -> Usage:
    """``TokenUsageBreakdown`` → neutral :class:`Usage` (input includes cached; output includes
    reasoning, as Codex reports them)."""
    return Usage(
        input=b.input_tokens,
        cached_input=b.cached_input_tokens,
        cache_write=b.cache_write_input_tokens or 0,
        output=b.output_tokens,
        reasoning=b.reasoning_output_tokens,
    )


def usage_delta(current: Usage, previous: Usage) -> Usage:
    """Field-wise ``current - previous`` clamped at zero (totals never decrease, but be safe)."""
    return Usage(
        input=max(current.input - previous.input, 0),
        cached_input=max(current.cached_input - previous.cached_input, 0),
        cache_write=max(current.cache_write - previous.cache_write, 0),
        output=max(current.output - previous.output, 0),
        reasoning=max(current.reasoning - previous.reasoning, 0),
    )


def _usage_dict(usage: Usage) -> dict[str, int]:
    return {
        "input": usage.input,
        "cached_input": usage.cached_input,
        "cache_write": usage.cache_write,
        "output": usage.output,
        "reasoning": usage.reasoning,
    }


def _env_signature(env: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in env.items()))


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    return value


def _effort(effort: str | None) -> ReasoningEffort | None:
    if effort is None:
        return None
    name = CODEX_CAPABILITIES.effort_aliases.get(effort, effort)
    if name not in CODEX_CAPABILITIES.effort_levels:  # ReasoningEffort is an open enum
        raise ProviderError(
            f"codex: unknown effort {effort!r}",
            hint=(
                "use one of none, minimal, low, medium, high, xhigh, max, ultra "
                "(max/ultra: gpt-5.6 family)"
            ),
        )
    return ReasoningEffort(name)


def _workflow_config(extra: Mapping[str, Any], warnings: list[str]) -> dict[str, Any]:
    """``provider_options.codex.config`` minus every path in :data:`ADAPTER_OWNED_CONFIG`.

    A dropped key is warned about rather than silently honoured or silently discarded: the
    workflow author has to learn that the neutral field — ``model:``, ``access:``, ``tools:``,
    ``network:`` — is the way to change it, and an operator reading the run has to be able to
    see that the attempt was made.
    """
    config = {str(k): v for k, v in extra.items()}
    for path in ADAPTER_OWNED_CONFIG:
        head, *rest = path
        if head not in config:
            continue
        spelled = ".".join(("provider_options", "codex", "config", *path))
        note = f"{spelled}: computed by the codex adapter from the agent's own fields; ignored"
        if not rest:
            del config[head]
            warnings.append(note)
            continue
        nested = config[head]
        if not isinstance(nested, Mapping):
            del config[head]  # cannot be narrowed, and it would replace the computed table
            warnings.append(
                f"provider_options.codex.config.{head}: must be a mapping to be merged; ignored"
            )
            continue
        trimmed = {str(k): v for k, v in nested.items() if str(k) != rest[0]}
        if len(trimmed) != len(nested):
            warnings.append(note)
        if trimmed:
            config[head] = trimmed
        else:
            del config[head]
    return config


def _mcp_config(req: AgentRequest) -> dict[str, Any]:
    """``req.mcp_servers`` → Codex ``mcp_servers`` config (stdio ``command`` or streamable-HTTP
    ``url``); malformed specs raise :class:`ProviderError` naming the server."""
    servers: dict[str, Any] = {}
    for spec in req.mcp_servers:
        if spec.transport == "stdio":
            entry: dict[str, Any] = {"command": spec.command or spec.name}
            if spec.args:
                entry["args"] = list(spec.args)
            if spec.env:
                entry["env"] = dict(spec.env)
        elif spec.transport == "http":
            if not spec.url:
                raise ProviderError(
                    f"codex: mcp server {spec.name!r} needs a url",
                    hint="mcp.<name>.url: https://... for transport http",
                )
            entry = {"url": spec.url}
            if spec.headers:
                entry["http_headers"] = dict(spec.headers)
        else:
            raise ProviderError(
                f"codex: mcp server {spec.name!r}: transport {spec.transport!r} is not supported "
                "by codex",
                hint="codex speaks stdio (command) and streamable http (url); sse is not supported",
            )
        servers[spec.name] = entry
    return servers


async def _discard(_event: AgentEvent) -> None:
    """Emit sink used to keep tracking a turn after the real sink failed."""
    return None


# --------------------------------------------------------------------------------------------------
# Per-turn streaming state
# --------------------------------------------------------------------------------------------------


@dataclass
class _TurnState:
    """Everything the notification mapper accumulates for one turn."""

    thread_id: str
    turn_id: str
    fresh_thread: bool
    usage: Usage = field(default_factory=Usage)
    deltas: dict[str, list[str]] = field(default_factory=dict)
    completed_messages: list[tuple[str | None, str, str]] = field(default_factory=list)
    reasoning_seen: set[str] = field(default_factory=set)
    tool_calls_seen: set[str] = field(default_factory=set)
    last_error: TurnError | None = None
    completed: TurnCompletedNotification | None = None

    def final_text(self) -> str:
        """``final_answer`` wins; else the last phase-less message; commentary is never output;
        else the concatenated deltas of messages that never completed (interrupted turns)."""
        phaseless: str | None = None
        for phase, _item_id, text in reversed(self.completed_messages):
            if phase == MessagePhase.final_answer.value:
                return text
            if phase is None and phaseless is None:
                phaseless = text
        if phaseless is not None:
            return phaseless
        done = {item_id for _phase, item_id, _text in self.completed_messages}
        partial = [
            "".join(chunks) for item_id, chunks in self.deltas.items() if item_id not in done
        ]
        return "".join(partial)


@dataclass
class _PooledClient:
    codex: Any  # AsyncCodex (or a test double)
    signature: tuple[tuple[str, str], ...]
    poisoned: bool = False


class _TurnRun:
    """Mutable cell shared between the shielded driver task and :meth:`CodexProvider._run`.

    ``started`` fires once the turn handle exists (or setup failed), ``settled`` once the stream
    finished *or* the sink raised, ``done`` once the driver returned (stream closed). ``error`` is
    what the SDK (or a bug) raised; ``sink_error`` is what ``emit`` raised.
    """

    __slots__ = ("done", "error", "handle", "settled", "sink_error", "started")

    def __init__(self) -> None:
        self.handle: Any = None
        self.started = anyio.Event()
        self.settled = anyio.Event()
        self.done = anyio.Event()
        self.error: BaseException | None = None
        self.sink_error: BaseException | None = None


# --------------------------------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------------------------------


class CodexProvider:
    """OpenAI Codex SDK provider (see the module docstring for the design and settings)."""

    id: str = "codex"
    capabilities: ProviderCapabilities = CODEX_CAPABILITIES

    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        self.settings: Mapping[str, Any] = dict(settings or {})
        self.approval_mode: str = str(
            self.settings.get("approval_mode", ApprovalMode.deny_all.value)
        )
        extra = self.settings.get("config") or {}
        if not isinstance(extra, Mapping):
            raise ProviderError(
                "codex settings: `config` must be a mapping",
                hint="providers.codex.config: {model_reasoning_summary: detailed, ...}",
            )
        self.extra_config: Mapping[str, Any] = dict(extra)
        self.codex_bin: str | None = (
            str(self.settings["codex_bin"]) if self.settings.get("codex_bin") else None
        )
        self.drain_s: float = float(self.settings.get("drain_s", DEFAULT_DRAIN_S))
        self.pricing: PriceTable = PriceTable.from_config(self.settings.get("pricing"))
        self.run_id: str = ""
        self.workdir: str | None = None
        self.env: Mapping[str, str] = {}
        self.max_parallel: int = 1
        self.executor_workers: int = 0
        self.limiter: anyio.CapacityLimiter | None = None
        self._clients: dict[tuple[tuple[str, str], ...], _PooledClient] = {}
        self._last_totals: dict[str, Usage] = {}

    # -- Provider protocol ---------------------------------------------------------------------

    async def open(
        self, *, run_id: str, workdir: str, env: Mapping[str, str], max_parallel: int
    ) -> None:
        """Record the run context, size the default executor and create the turn limiter.

        Clients are created lazily per environment signature on first :meth:`run`.
        """
        self.run_id = run_id
        self.workdir = workdir or None
        self.env = dict(env)
        self.max_parallel = max(1, int(max_parallel))
        self._ensure_workers()

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        """Run one turn (new, resumed or forked thread) and map its stream to neutral events."""
        self._ensure_workers()
        assert self.limiter is not None
        async with self.limiter:
            return await self._run(req, emit)

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
        """SDK version, bundled CLI path + ``--version``, auth (env key or ``account()``),
        optional 1-turn probe (read-only, deny-all, ephemeral)."""
        details: list[str] = []
        ok = True
        cli_path: str | None = None
        cli_version: str | None = None
        try:
            cli_path = self._resolve_cli_path()
        except ProviderNotInstalledError as exc:
            ok = False
            details.append(f"codex runtime: {exc} ({exc.hint})")
        if cli_path is not None:
            cli_version = await self._cli_version(cli_path, details)
        auth = await self._auth_state(details)
        if auth == "missing":
            ok = False
        if probe and ok:
            ok = await self._probe(details)
        return ProviderHealth(
            ok=ok,
            sdk_version=_SDK_VERSION,
            cli_path=cli_path,
            cli_version=cli_version,
            auth=auth,
            details=tuple(details),
        )

    async def aclose(self) -> None:
        """Close every pooled ``codex app-server`` client (shielded from cancellation) and forget
        the per-thread usage totals (the engine carries them via ``raw["usage_total"]``)."""
        clients = list(self._clients.values())
        self._clients.clear()
        self._last_totals.clear()
        with anyio.CancelScope(shield=True):
            for client in clients:
                await self._close_client(client)

    # -- workers / pool --------------------------------------------------------------------------

    def _ensure_workers(self) -> None:
        """Raise the loop's default executor to the needed size and (re)create the limiter."""
        needed = max(MIN_EXECUTOR_WORKERS, 2 * self.max_parallel + 8)
        size = needed
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - only reachable outside an event loop
            loop = None
        if loop is not None:
            # Process-global side effect shared with every provider/run on this loop: the SDK's
            # asyncio.to_thread calls land on the loop's default executor. engine/runtime.py
            # sizes it up front; this is the fallback for standalone use. Reading the current
            # size relies on CPython asyncio's private ``_default_executor``/``_max_workers``.
            current = getattr(loop, "_default_executor", None)
            current_size = getattr(current, "_max_workers", None)
            if isinstance(current_size, int) and current_size >= needed:
                size = current_size
            else:
                loop.set_default_executor(
                    ThreadPoolExecutor(max_workers=needed, thread_name_prefix="rayspec-codex")
                )
                if isinstance(current, ThreadPoolExecutor):
                    current.shutdown(wait=False)  # in-flight work finishes; nothing leaks
        if self.limiter is None or size != self.executor_workers:
            self.executor_workers = size
            tokens = max(1, size - SPARE_WORKERS)
            if self.limiter is None:
                self.limiter = anyio.CapacityLimiter(tokens)
            else:
                self.limiter.total_tokens = tokens

    def _merged_env(self, req_env: Mapping[str, str]) -> dict[str, str]:
        return {**self.env, **{str(k): str(v) for k, v in req_env.items()}}

    def _client_for(self, req_env: Mapping[str, str]) -> _PooledClient:
        """Pooled client for the merged environment; poisoned clients are replaced."""
        merged = self._merged_env(req_env)
        signature = _env_signature(merged)
        pooled = self._clients.get(signature)
        if pooled is not None and not pooled.poisoned:
            return pooled
        config = CodexConfig(
            codex_bin=self.codex_bin,
            cwd=self.workdir,
            env=merged,
            client_name="rayspec",
            client_version=__version__,
        )
        pooled = _PooledClient(codex=AsyncCodex(config), signature=signature)
        self._clients[signature] = pooled
        return pooled

    async def _close_client(self, pooled: _PooledClient) -> None:
        if pooled.poisoned:
            return  # already closed (idempotent: abort + stream error may both poison)
        pooled.poisoned = True
        try:
            await pooled.codex.close()
        except Exception as exc:  # closing a dead process must never mask the real failure
            log.debug("codex client close failed: %s", exc)

    async def _poison(self, pooled: _PooledClient) -> None:
        """Mark ``pooled`` dead, close it and drop it from the pool (shielded)."""
        if self._clients.get(pooled.signature) is pooled:
            del self._clients[pooled.signature]
        with anyio.CancelScope(shield=True):
            await self._close_client(pooled)

    # -- request → SDK kwargs --------------------------------------------------------------------

    @staticmethod
    def _options(req: AgentRequest) -> Mapping[str, Any]:
        """``provider_options`` narrowed to codex (accepts ``{codex: {...}}`` or the inner map)."""
        opts = req.provider_options or {}
        inner = opts.get("codex")
        if isinstance(inner, Mapping):
            return inner
        return opts

    def _approval_mode(self, opts: Mapping[str, Any]) -> ApprovalMode:
        raw = opts.get("approval_mode", self.approval_mode)
        try:
            return ApprovalMode(str(raw))
        except ValueError:
            raise ProviderError(
                f"codex: unknown approval_mode {raw!r}",
                hint="use approval_mode: deny_all (default) or auto_review",
            ) from None

    def _thread_kwargs(
        self, req: AgentRequest, opts: Mapping[str, Any], warnings: list[str]
    ) -> dict[str, Any]:
        translation = translate_tools(req.tools.allow, req.tools.deny, self.id, self.capabilities)
        if translation.errors:
            raise ProviderError(
                "codex: unsupported tool policy: " + "; ".join(translation.errors),
                hint="codex only honours tools.deny: [web]; drop the other entries",
            )
        extra = opts.get("config") or {}
        if not isinstance(extra, Mapping):
            raise ProviderError("codex: provider_options.config must be a mapping")
        config: dict[str, Any] = {**self.extra_config, **_workflow_config(extra, warnings)}
        if req.mcp_servers:
            existing = config.get("mcp_servers") or {}
            if not isinstance(existing, Mapping):
                raise ProviderError(
                    "codex: config.mcp_servers must be a mapping of server name -> config",
                    hint="providers.codex.config.mcp_servers: {name: {command: ...}}",
                )
            config["mcp_servers"] = {**existing, **_mcp_config(req)}
        config.update(translation.config_overrides)
        kwargs: dict[str, Any] = {
            "cwd": req.cwd,
            "model": req.model,
            "sandbox": _SANDBOX[AccessLevel(req.access)],
            "approval_mode": self._approval_mode(opts),
            "config": config,
        }
        if req.instructions is not None:
            key = (
                "base_instructions"
                if req.instructions_mode == "replace"
                else "developer_instructions"
            )
            kwargs[key] = req.instructions
        return kwargs

    async def _open_thread(self, codex: Any, req: AgentRequest, kwargs: dict[str, Any]) -> Any:
        if req.resume_session and req.fork_session:
            return await codex.thread_fork(req.resume_session, **kwargs)
        if req.resume_session:
            return await codex.thread_resume(req.resume_session, **kwargs)
        opts = self._options(req)
        if opts.get("ephemeral"):
            kwargs = {**kwargs, "ephemeral": True}
        return await codex.thread_start(**kwargs)

    # -- the turn ----------------------------------------------------------------------------------

    def _seed_baseline(self, thread_id: str, opts: Mapping[str, Any]) -> None:
        """Adopt ``provider_options.codex.usage_baseline`` for a resumed thread this instance has
        not seen yet (its own observations are always at least as recent)."""
        raw = opts.get("usage_baseline")
        if raw is None:
            return
        if not isinstance(raw, Mapping):
            raise ProviderError(
                "codex: provider_options.usage_baseline must be a mapping of usage counters",
                hint="pass AgentResult.raw['usage_total'] of the previous turn on this thread",
            )
        if thread_id in self._last_totals:
            return
        try:
            self._last_totals[thread_id] = Usage(
                input=int(raw.get("input", 0)),
                cached_input=int(raw.get("cached_input", 0)),
                cache_write=int(raw.get("cache_write", 0)),
                output=int(raw.get("output", 0)),
                reasoning=int(raw.get("reasoning", 0)),
            )
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                f"codex: provider_options.usage_baseline has a non-integer counter: {exc}",
                hint="pass AgentResult.raw['usage_total'] of the previous turn on this thread",
            ) from None

    async def _run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        started = time.perf_counter()
        opts = self._options(req)
        option_warnings: list[str] = []
        kwargs = self._thread_kwargs(req, opts, option_warnings)
        for warning in option_warnings:
            await emit(AgentEvent(kind="warning", text=warning, name="provider_options"))
        effort = _effort(req.effort)
        output_schema: dict[str, Any] | None = None
        if req.output_schema is not None:
            output_schema, schema_warnings = for_openai_strict(req.output_schema)
            for warning in schema_warnings:
                await emit(AgentEvent(kind="warning", text=warning, name="output_schema"))
        if req.resume_session:
            self._seed_baseline(req.resume_session, opts)
        pooled = self._client_for(req.env)
        state = _TurnState(thread_id="", turn_id="", fresh_thread=not req.resume_session)
        cell = _TurnRun()
        timed_out = False

        async def drive() -> None:
            # Everything that can block a worker (thread start, turn start, the stream) runs
            # here, shielded: the parent decides what to do on timeout/cancel/sink failure.
            with anyio.CancelScope(shield=True):
                try:
                    thread = await self._open_thread(pooled.codex, req, kwargs)
                    state.thread_id = str(thread.id)
                    handle = await thread.turn(
                        req.prompt, output_schema=output_schema, effort=effort, model=req.model
                    )
                    cell.handle = handle
                    state.turn_id = str(handle.id)
                    cell.started.set()
                    async with contextlib.aclosing(handle.stream()) as stream:
                        async for notification in stream:
                            if cell.sink_error is not None:  # keep bookkeeping, drop events
                                await self._on_notification(notification, state, _discard)
                                continue
                            try:
                                await self._on_notification(notification, state, emit)
                            except BaseException as exc:
                                cell.sink_error = exc
                                cell.settled.set()  # the parent interrupts the turn
                except BaseException as exc:  # recorded here, decided by the parent
                    cell.error = exc
                finally:
                    cell.started.set()
                    cell.done.set()
                    cell.settled.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(drive)
            try:
                with anyio.fail_after(req.timeout_s):
                    await cell.settled.wait()
                if cell.sink_error is not None:
                    await self._abort_turn(cell, pooled, reason="sink failure")
            except TimeoutError:
                timed_out = True
                await self._abort_turn(cell, pooled, reason="timeout")
            except anyio.get_cancelled_exc_class():
                await self._abort_turn(cell, pooled, reason="cancellation")
                raise

        duration_ms = int((time.perf_counter() - started) * 1000)
        error = cell.error
        if isinstance(error, TransportClosedError | CodexError):
            await self._poison(pooled)
        if cell.sink_error is not None:
            raise cell.sink_error  # engine/store failure: never dressed up as a codex error
        if error is not None:
            if timed_out:  # secondary to our own deadline (e.g. the transport we closed)
                log.debug("codex turn %s ended with %r after timeout", state.turn_id, error)
            else:
                translated = (
                    await self._translate(error, pooled, setup=cell.handle is None)
                    if isinstance(error, Exception)
                    else None
                )
                if translated is None:
                    raise error
                raise translated from error
        result = self._result(req, state, timed_out=timed_out, duration_ms=duration_ms)
        if result.raw.get("deadline_exceeded"):
            await emit(
                AgentEvent(
                    kind="warning",
                    text=(
                        f"codex turn exceeded {req.timeout_s}s but completed before the "
                        "interrupt took effect; keeping its answer"
                    ),
                    name="deadline",
                )
            )
        return result

    async def _abort_turn(self, cell: _TurnRun, pooled: _PooledClient, *, reason: str) -> None:
        """Interrupt (shielded) → bounded drain → close + recreate the client if still hung.

        Works in every phase: a driver still inside ``thread_start``/``turn()`` gets ``drain_s``
        to come back (then the client is closed to wake its worker); a started turn is
        interrupted and drained for ``drain_s`` (then the client is closed likewise).
        """
        with anyio.CancelScope(shield=True):
            if cell.done.is_set():
                return
            if not cell.started.is_set():
                with anyio.move_on_after(self.drain_s):
                    await cell.started.wait()
                if not cell.started.is_set():
                    log.warning(
                        "codex thread/turn start did not return %.1fs after %s; closing the client",
                        self.drain_s,
                        reason,
                    )
                    await self._poison(pooled)
                    await cell.done.wait()
                    return
            if cell.done.is_set() or cell.handle is None:
                return  # setup failed on its own: nothing to interrupt
            try:
                await cell.handle.interrupt()
            except Exception as exc:  # transport may already be dead
                log.debug("codex interrupt after %s failed: %s", reason, exc)
            with anyio.move_on_after(self.drain_s):
                await cell.done.wait()
            if cell.done.is_set():
                return
            log.warning(
                "codex turn %s did not finish %.1fs after interrupt (%s); closing the client",
                cell.handle.id,
                self.drain_s,
                reason,
            )
            await self._poison(pooled)
            await cell.done.wait()

    async def _translate(
        self, exc: Exception, pooled: _PooledClient, *, setup: bool
    ) -> ProviderError | None:
        """SDK exception → :class:`ProviderError` (poisoning the client when its transport died).

        ``setup`` is True for exceptions raised by ``thread_start``/``turn()`` (where a
        ``ValueError`` means a bad request parameter). Returns ``None`` for exceptions that are
        not the SDK's (bugs, emit failures): callers re-raise those untouched.
        """
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, FileNotFoundError):
            return ProviderNotInstalledError(
                f"codex runtime not found: {exc}",
                hint=(
                    "install the openai-codex-cli-bin package (pip install openai-codex) or set "
                    "providers.codex.codex_bin to a codex binary"
                ),
            )
        if isinstance(exc, TransportClosedError):
            await self._poison(pooled)
            return ProviderError(
                f"codex app-server transport closed: {exc}", transient=True, kind="transport"
            )
        if isinstance(exc, ServerBusyError) or is_retryable_error(exc):
            return ProviderError(f"codex server busy: {exc}", transient=True, kind="api")
        if isinstance(exc, JsonRpcError):
            return ProviderError(f"codex request failed: {exc.message}", kind="api")
        if isinstance(exc, CodexError):
            return ProviderError(f"codex error: {exc}", kind="api")
        if setup and isinstance(exc, ValueError):  # bad sandbox/approval/model values
            return ProviderError(f"codex: invalid request parameter: {exc}", kind="provider")
        return None

    # -- notifications → events ------------------------------------------------------------------

    async def _on_notification(
        self, notification: Notification, state: _TurnState, emit: EmitFn
    ) -> None:
        method = notification.method
        payload = notification.payload
        if isinstance(payload, TurnStartedNotification):
            await emit(
                AgentEvent(
                    kind="session",
                    text=state.thread_id,
                    data={"thread_id": state.thread_id, "turn_id": payload.turn.id},
                )
            )
        elif isinstance(payload, AgentMessageDeltaNotification):
            state.deltas.setdefault(payload.item_id, []).append(payload.delta)
            await emit(
                AgentEvent(kind="text_delta", text=payload.delta, data={"item_id": payload.item_id})
            )
        elif isinstance(payload, ReasoningSummaryTextDeltaNotification):
            state.reasoning_seen.add(payload.item_id)
            await emit(
                AgentEvent(
                    kind="reasoning",
                    text=payload.delta,
                    data={"item_id": payload.item_id, "part": "summary"},
                )
            )
        elif isinstance(payload, ReasoningTextDeltaNotification):
            state.reasoning_seen.add(payload.item_id)
            await emit(
                AgentEvent(
                    kind="reasoning",
                    text=payload.delta,
                    data={"item_id": payload.item_id, "part": "text"},
                )
            )
        elif isinstance(payload, CommandExecutionOutputDeltaNotification):
            await emit(
                AgentEvent(kind="command_output", text=payload.delta, call_id=payload.item_id)
            )
        elif isinstance(payload, ItemStartedNotification):
            await self._on_item(payload.item.root, state, emit, completed=False)
        elif isinstance(payload, ItemCompletedNotification):
            await self._on_item(payload.item.root, state, emit, completed=True)
        elif isinstance(payload, TurnPlanUpdatedNotification):
            steps = [{"step": s.step, "status": s.status.value} for s in payload.plan]
            text = "\n".join(f"[{s['status']}] {s['step']}" for s in steps)
            await emit(
                AgentEvent(
                    kind="plan", text=text, data={"plan": steps, "explanation": payload.explanation}
                )
            )
        elif isinstance(payload, ThreadTokenUsageUpdatedNotification):
            await self._on_usage(payload, state, emit)
        elif isinstance(payload, ErrorNotification):
            state.last_error = payload.error
            data = {
                "will_retry": payload.will_retry,
                "codex_error_info": error_info_code(payload.error),
            }
            kind = "warning" if payload.will_retry else "error"
            await emit(AgentEvent(kind=kind, text=payload.error.message, data=data))
        elif isinstance(payload, TurnCompletedNotification):
            state.completed = payload
        else:
            raw = payload.params if isinstance(payload, UnknownNotification) else _jsonable(payload)
            await emit(AgentEvent(kind="raw", name=method, text=method, data={"payload": raw}))

    async def _on_item(
        self, item: Any, state: _TurnState, emit: EmitFn, *, completed: bool
    ) -> None:
        if isinstance(item, AgentMessageThreadItem):
            if completed:
                phase = item.phase.value if item.phase is not None else None
                state.completed_messages.append((phase, item.id, item.text))
                await emit(
                    AgentEvent(
                        kind="text", text=item.text, data={"item_id": item.id, "phase": phase}
                    )
                )
        elif isinstance(item, ReasoningThreadItem):
            if completed and item.id not in state.reasoning_seen:
                text = "\n".join(item.summary or []) or "\n".join(item.content or [])
                if text:
                    await emit(AgentEvent(kind="reasoning", text=text, data={"item_id": item.id}))
        elif isinstance(item, CommandExecutionThreadItem):
            cwd = item.cwd.root if hasattr(item.cwd, "root") else str(item.cwd)
            if not completed:
                await emit(
                    AgentEvent(
                        kind="command_start",
                        text=item.command,
                        name=item.command,
                        call_id=item.id,
                        data={"command": item.command, "cwd": cwd},
                    )
                )
            else:
                await emit(
                    AgentEvent(
                        kind="command_end",
                        text=item.aggregated_output or "",
                        name=item.command,
                        call_id=item.id,
                        data={
                            "command": item.command,
                            "cwd": cwd,
                            "exit_code": item.exit_code,
                            "status": item.status.value,
                            "duration_ms": item.duration_ms,
                        },
                    )
                )
        elif isinstance(item, FileChangeThreadItem):
            if completed:
                for change in item.changes:
                    kind_root = change.kind.root
                    await emit(
                        AgentEvent(
                            kind="file_change",
                            text=change.diff,
                            name=change.path,
                            call_id=item.id,
                            data={
                                "path": change.path,
                                "kind": kind_root.type,
                                "diff": change.diff,
                                "status": item.status.value,
                                "move_path": getattr(kind_root, "move_path", None),
                            },
                        )
                    )
        elif isinstance(item, McpToolCallThreadItem):
            name = f"{item.server}/{item.tool}"
            if not completed or item.id not in state.tool_calls_seen:
                state.tool_calls_seen.add(item.id)
                await emit(
                    AgentEvent(
                        kind="tool_call",
                        name=name,
                        call_id=item.id,
                        data={
                            "server": item.server,
                            "tool": item.tool,
                            "arguments": item.arguments,
                        },
                    )
                )
            if completed:
                result = _jsonable(item.result) if item.result is not None else None
                text = json.dumps(result["content"]) if result and "content" in result else ""
                await emit(
                    AgentEvent(
                        kind="tool_result",
                        name=name,
                        call_id=item.id,
                        text=item.error.message if item.error is not None else text,
                        data={
                            "status": item.status.value,
                            "error": item.error.message if item.error is not None else None,
                            "result": result,
                            "duration_ms": item.duration_ms,
                        },
                    )
                )
        elif isinstance(item, WebSearchThreadItem):
            if not completed or item.id not in state.tool_calls_seen:
                state.tool_calls_seen.add(item.id)
                await emit(
                    AgentEvent(
                        kind="tool_call",
                        name="web_search",
                        text=item.query,
                        call_id=item.id,
                        data={"query": item.query},
                    )
                )
            if completed:
                await emit(
                    AgentEvent(
                        kind="tool_result",
                        name="web_search",
                        text=item.query,
                        call_id=item.id,
                        data={
                            "query": item.query,
                            "action": _jsonable(item.action) if item.action else None,
                            "results": _jsonable(item.results),
                        },
                    )
                )
        elif isinstance(item, PlanThreadItem):
            if completed:
                await emit(AgentEvent(kind="plan", text=item.text, data={"item_id": item.id}))
        else:
            await emit(
                AgentEvent(
                    kind="raw",
                    name=getattr(item, "type", type(item).__name__),
                    data={"completed": completed, "item": _jsonable(item)},
                )
            )

    async def _on_usage(
        self, payload: ThreadTokenUsageUpdatedNotification, state: _TurnState, emit: EmitFn
    ) -> None:
        thread_id = payload.thread_id
        total = usage_from_breakdown(payload.token_usage.total)
        previous = self._last_totals.get(thread_id)
        if previous is None:
            if state.fresh_thread:
                previous = Usage()
            else:  # history before this turn: everything except the last request
                previous = usage_delta(total, usage_from_breakdown(payload.token_usage.last))
        delta = usage_delta(total, previous)
        self._last_totals[thread_id] = total
        state.usage = state.usage + delta
        await emit(
            AgentEvent(
                kind="usage",
                data={
                    "usage": _usage_dict(delta),
                    "total": _usage_dict(total),
                    "turn_total": _usage_dict(state.usage),
                    "model_context_window": payload.token_usage.model_context_window,
                },
            )
        )

    # -- result ------------------------------------------------------------------------------------

    def _result(
        self, req: AgentRequest, state: _TurnState, *, timed_out: bool, duration_ms: int
    ) -> AgentResult:
        text = state.final_text()
        structured: Any = None
        if req.output_schema is not None and text:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                structured = None
        turn = state.completed.turn if state.completed is not None else None
        turn_status = turn.status if turn is not None else None
        status: ResultStatus
        error: AgentError | None = None
        deadline_exceeded = False
        if turn_status is TurnStatus.completed:
            status = "success"  # a valid answer is never discarded, even after our deadline
            deadline_exceeded = timed_out
        elif timed_out:
            status = "timeout"
            error = AgentError(
                kind="timeout",
                message=f"codex turn exceeded {req.timeout_s}s and was interrupted",
                transient=False,
            )
        elif turn_status is TurnStatus.interrupted:
            status = "interrupted"
        elif turn_status is TurnStatus.failed:
            status = "error"
            error = classify_turn_error(
                turn.error if turn is not None and turn.error is not None else state.last_error
            )
        else:  # stream ended without turn/completed (only reachable after a handled abort)
            status = "error"
            error = AgentError(
                kind="unknown", message="codex turn ended without turn/completed", transient=True
            )
        denials = turn_denials(turn)
        cost = self.pricing.cost_usd(req.model, state.usage)
        total = self._last_totals.get(state.thread_id) if state.thread_id else None
        raw: dict[str, Any] = {
            "thread_id": state.thread_id or None,
            "turn_id": state.turn_id or None,
            "turn_status": turn_status.value if turn_status is not None else None,
            "turn_error": _jsonable(turn.error) if turn is not None and turn.error else None,
            "timed_out": timed_out,
            "usage_total": _usage_dict(total) if total is not None else None,
        }
        if deadline_exceeded:
            raw["deadline_exceeded"] = True
        if turn is not None and turn.duration_ms is not None:
            raw["turn_duration_ms"] = turn.duration_ms
        return AgentResult(
            status=status,
            text=text,
            structured=structured,
            session_ref=state.thread_id or None,
            usage=state.usage,
            cost_usd=cost,
            cost_source="table" if cost is not None else "none",
            duration_ms=duration_ms,
            num_turns=1,
            model=req.model,  # as requested: the SDK handle does not expose the effective model
            error=error,
            denials=denials,
            raw=raw,
        )

    # -- healthcheck pieces ----------------------------------------------------------------------

    def _resolve_cli_path(self) -> str:
        if self.codex_bin:
            if not os.path.exists(self.codex_bin):
                raise ProviderNotInstalledError(
                    f"codex binary not found at {self.codex_bin}",
                    hint="fix providers.codex.codex_bin or remove it to use the bundled runtime",
                )
            return self.codex_bin
        try:
            from codex_cli_bin import bundled_codex_path
        except ImportError as exc:
            raise ProviderNotInstalledError(
                "bundled codex runtime (openai-codex-cli-bin) is not installed",
                hint="pip install openai-codex (pulls openai-codex-cli-bin) or set codex_bin",
            ) from exc
        try:
            return str(bundled_codex_path())
        except FileNotFoundError as exc:
            raise ProviderNotInstalledError(
                f"bundled codex runtime is broken: {exc}",
                hint="reinstall openai-codex-cli-bin or set providers.codex.codex_bin",
            ) from exc

    async def _cli_version(self, cli_path: str, details: list[str]) -> str | None:
        try:
            with anyio.fail_after(20):
                proc = await anyio.run_process([cli_path, "--version"], check=False)
        except Exception as exc:  # missing exec bit, timeout, ...
            details.append(f"codex --version failed: {type(exc).__name__}: {exc}")
            return None
        out = (proc.stdout or b"").decode(errors="replace").strip()
        if proc.returncode != 0:
            details.append(f"codex --version exited {proc.returncode}: {out[:200]}")
            return None
        return out.split()[-1] if out else None

    async def _auth_state(self, details: list[str]) -> Literal["ok", "missing", "unknown"]:
        if os.environ.get("OPENAI_API_KEY"):
            details.append("auth: OPENAI_API_KEY set")
            return "ok"
        pooled = self._client_for({})
        try:
            account = await pooled.codex.account()
        except Exception as exc:
            details.append(f"auth: account lookup failed: {type(exc).__name__}: {exc}")
            return "unknown"
        if account.account is not None:
            root = account.account.root
            details.append(f"auth: codex login ({getattr(root, 'type', 'account')})")
            return "ok"
        details.append("auth: no codex login (run `codex login` or set OPENAI_API_KEY)")
        return "missing"

    async def _probe(self, details: list[str]) -> bool:
        req = AgentRequest(
            step_path="__probe__",
            prompt=PROBE_PROMPT,
            cwd=self.workdir or os.getcwd(),
            access=AccessLevel.READ_ONLY,
            timeout_s=PROBE_TIMEOUT_S,
            provider_options={"codex": {"ephemeral": True}},
        )

        async def discard(_event: AgentEvent) -> None:
            return None

        try:
            result = await self.run(req, discard)
        except Exception as exc:
            details.append(f"probe failed: {type(exc).__name__}: {exc}")
            return False
        if result.status != "success":
            message = result.error.message if result.error else result.status
            details.append(f"probe failed: {result.status}: {message}")
            return False
        details.append(f"probe: ok ({result.text.strip()[:40]!r})")
        return True


__all__ = [
    "ADAPTER_OWNED_CONFIG",
    "DEFAULT_DRAIN_S",
    "CodexProvider",
    "classify_turn_error",
    "error_info_code",
    "usage_delta",
    "usage_from_breakdown",
]
