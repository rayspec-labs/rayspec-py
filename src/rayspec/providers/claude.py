# SPDX-License-Identifier: Apache-2.0
"""Claude Agent SDK adapter: :class:`ClaudeProvider` (provider id ``claude``).

Boundary: the only rayspec module that imports ``claude_agent_sdk``. It translates a neutral
:class:`~rayspec.providers.base.AgentRequest` into :class:`claude_agent_sdk.ClaudeAgentOptions`,
runs one ``query()`` per request (one ``claude`` subprocess each), maps the SDK message stream onto
:class:`~rayspec.providers.base.AgentEvent` and folds the ``ResultMessage`` into an
:class:`~rayspec.providers.base.AgentResult`. Structured output is *not* parsed here (the engine
owns ``engine/structured.py``); the adapter only passes the schema and returns
``ResultMessage.structured_output``.

Settings (``config.providers.claude`` → ``ClaudeProvider(settings)``):

* ``setting_sources``: list of ``user|project|local`` (default ``["project"]``; ``null`` loads all)
* ``cli_path``: explicit ``claude`` binary (default: bundled CLI → ``PATH`` → known locations)
* ``env``: extra environment for the ``claude`` subprocess (below the request env)

Malformed settings raise :class:`~rayspec.providers.base.ProviderError` (``kind="provider"``) at
construction with a hint naming the ``providers.claude.<key>`` to fix.

Cancellation discipline: ``query()`` is consumed under :func:`contextlib.aclosing` inside
``anyio.fail_after(req.timeout_s)``; anyio-originated cancellation reaches the generator at
``__anext__`` and the SDK closes the transport inside its own shield (stdin EOF → SIGTERM →
SIGKILL). ``CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK`` is set (``setdefault``) on the *parent*
environment at construction, which is where the SDK reads it.
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
import shutil
import subprocess
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, cast

import anyio
import claude_agent_sdk
from anyio import to_thread
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    ResultError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from claude_agent_sdk.types import (
    McpHttpServerConfig,
    McpServerConfig,
    McpSSEServerConfig,
    McpStdioServerConfig,
    SystemPromptPreset,
    ThinkingConfig,
)

from rayspec import __version__
from rayspec.providers._tools import ToolTranslation, translate_tools
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
    usage_dict,
)
from rayspec.providers.capabilities import CLAUDE_CAPABILITIES

#: ``claude_agent_sdk.__version__`` (re-exported for ``rayspec doctor``).
SDK_VERSION: str = claude_agent_sdk.__version__


def _bundled_cli_version() -> str | None:
    """``claude_agent_sdk._cli_version.__cli_version__`` (absent on SDK builds without a CLI)."""
    try:
        from claude_agent_sdk._cli_version import __cli_version__
    except ImportError:
        return None
    return str(__cli_version__) if __cli_version__ else None


#: Bundled Claude Code CLI version the installed SDK ships (``None`` if the SDK has none).
CLI_BUNDLED_VERSION: str | None = _bundled_cli_version()

#: Number of trailing stderr lines kept per request for error enrichment.
STDERR_TAIL_LINES = 40
#: HTTP statuses of an API failure worth retrying.
TRANSIENT_API_STATUSES: frozenset[int] = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
#: ``AssistantMessage.error`` values of a ``<synthetic>`` message that mean "try again".
TRANSIENT_SYNTHETIC_ERRORS: frozenset[str] = frozenset({"rate_limit", "server_error"})
#: ``AssistantMessage.error`` values that can never succeed on retry.
FATAL_SYNTHETIC_ERRORS: frozenset[str] = frozenset(
    {"authentication_failed", "billing_error", "invalid_request"}
)
#: Built-in tools available under ``access: read-only``.
READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
#: Built-in web tools, enabled only when the ``web`` group is allowed.
WEB_TOOLS: tuple[str, ...] = ("WebFetch", "WebSearch")
#: Default ``setting_sources`` (CLAUDE.md needs ``project``; user settings stay out of runs).
DEFAULT_SETTING_SOURCES: tuple[str, ...] = ("project",)
#: Prompt used by ``healthcheck(probe=True)``.
PROBE_PROMPT = "Reply with exactly OK"
#: Environment variables that prove credentials are configured (else the CLI login is consulted).
AUTH_ENV_VARS: tuple[str, ...] = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
#: macOS keychain item Claude Code keeps its claude.ai login in (checked for *existence* only).
CLI_LOGIN_KEYCHAIN_SERVICE = "Claude Code-credentials"
#: Bound for the ``security find-generic-password`` lookup (a hung keychain must not block us).
KEYCHAIN_LOOKUP_TIMEOUT_S = 5.0
#: Where the SDK looks for a system ``claude`` after the bundled binary and ``PATH`` (POSIX; on
#: Windows the SDK only probes ``~/.local/bin/claude.exe``, see :func:`_known_cli_locations`).
_KNOWN_CLI_LOCATIONS: tuple[Path, ...] = (
    Path.home() / ".npm-global/bin/claude",
    Path("/usr/local/bin/claude"),
    Path.home() / ".local/bin/claude",
    Path.home() / "node_modules/.bin/claude",
    Path.home() / ".yarn/bin/claude",
    Path.home() / ".claude/local/claude",
)
_OPTION_FIELDS: frozenset[str] = frozenset(f.name for f in fields(ClaudeAgentOptions))
#: ``ClaudeAgentOptions`` fields the adapter owns; ``provider_options`` may not override them.
ADAPTER_OWNED_OPTIONS: frozenset[str] = frozenset(
    {
        "stderr",
        "cwd",
        "cli_path",
        "resume",
        "fork_session",
        "output_format",
        "include_partial_messages",
    }
)
#: Dict-valued ``provider_options`` merged *under* the computed value instead of replacing it.
MERGED_OPTIONS: frozenset[str] = frozenset({"env", "mcp_servers"})
#: Valid ``providers.claude.setting_sources`` entries.
VALID_SETTING_SOURCES: frozenset[str] = frozenset({"user", "project", "local"})
_VERSION_RE = re.compile(r"([0-9]+\.[0-9]+\.[0-9]+)")
_INSTALL_HINT = (
    "install Claude Code (`npm install -g @anthropic-ai/claude-code` or the bundled SDK wheel) "
    "or set providers.claude.cli_path in config.yaml (`rayspec providers` lists the adapters)"
)


def _now() -> float:
    return time.time()


def _open_stream(prompt: str, options: ClaudeAgentOptions) -> AsyncGenerator[Any, None]:
    """Start ``query()`` (module global, so tests can monkeypatch it) as a closable generator."""
    return cast("AsyncGenerator[Any, None]", query(prompt=prompt, options=options))


# --------------------------------------------------------------------------------------------------
# Option building
# --------------------------------------------------------------------------------------------------


def _mcp_config(spec: Any) -> McpServerConfig:
    """Neutral :class:`McpServerSpec` → SDK TypedDict (stdio / http / sse)."""
    if spec.transport == "stdio":
        stdio: McpStdioServerConfig = {"type": "stdio", "command": spec.command or ""}
        if spec.args:
            stdio["args"] = list(spec.args)
        if spec.env:
            stdio["env"] = dict(spec.env)
        return stdio
    if spec.transport == "http":
        http: McpHttpServerConfig = {"type": "http", "url": spec.url or ""}
        if spec.headers:
            http["headers"] = dict(spec.headers)
        return http
    sse: McpSSEServerConfig = {"type": "sse", "url": spec.url or ""}
    if spec.headers:
        sse["headers"] = dict(spec.headers)
    return sse


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_options(
    provider: ClaudeProvider,
    req: AgentRequest,
    stderr: Callable[[str], None],
    *,
    run_env: Mapping[str, str] | None = None,
) -> tuple[ClaudeAgentOptions, ToolTranslation]:
    """Translate an :class:`AgentRequest` into :class:`ClaudeAgentOptions`.

    Returns the options and the tool translation (its ``warnings`` are emitted by ``run()``; an
    unknown ``provider_options`` key is appended to them). Raises :class:`ProviderError`
    (non-transient) when the cwd does not exist or the tool policy cannot be translated.

    Access mapping (plan §3.1): ``read-only`` → ``tools=[Read,Glob,Grep(+web)]``, same
    ``allowed_tools``, ``permission_mode="dontAsk"``; ``workspace-write`` → ``acceptEdits`` with
    ``allowed_tools=[Bash(+web)]``; ``full`` → ``bypassPermissions``. ``tools.deny`` always lands in
    ``disallowed_tools``. The bare ``mcp`` group expands to ``mcp__<server>`` over
    ``req.mcp_servers`` (Claude Code has no MCP wildcard). ``instructions_mode: append`` keeps the
    Claude Code preset prompt; ``replace`` passes the bare string (vanilla Claude: no Claude Code
    system prompt, CLAUDE.md is still loaded through ``setting_sources``).

    ``provider_options``: keys in :data:`ADAPTER_OWNED_OPTIONS` are ignored with a warning (they
    would break cancellation, env injection or resume); :data:`MERGED_OPTIONS` (``env``,
    ``mcp_servers``) are merged under the computed mapping (``env`` precedence: CLIENT_APP <
    settings.env < provider_options.env < open(env) < req.env); every other
    ``ClaudeAgentOptions`` field is applied verbatim; unknown keys warn.
    """
    if not Path(req.cwd).is_dir():
        raise ProviderError(
            f"working directory does not exist: {req.cwd}",
            kind="provider",
            hint="the step cwd must be an existing directory (check `cwd:` and the workspace)",
        )
    tr = translate_tools(req.tools.allow, req.tools.deny, provider.id, provider.capabilities)
    if not tr.ok:
        raise ProviderError(
            "tool policy not supported by provider 'claude': " + "; ".join(tr.errors),
            kind="provider",
            hint="fix tools.allow / tools.deny on the agent (see `rayspec validate`)",
        )
    warnings = list(tr.warnings)
    server_names = [s.name for s in req.mcp_servers]
    mcp_all = [f"mcp__{name}" for name in server_names]
    web_allowed = any(name in WEB_TOOLS for name in tr.allow_native)
    web = list(WEB_TOOLS) if web_allowed else []
    mcp_allowed = [n for n in tr.allow_native if n.startswith("mcp__")]
    if tr.allow_all_mcp:
        mcp_allowed.extend(mcp_all)
    disallowed = list(tr.deny_native)
    if tr.deny_all_mcp:
        disallowed.extend(mcp_all)

    tools: list[str] | None = None
    if req.access is AccessLevel.READ_ONLY:
        permission_mode = "dontAsk"
        tools = [*READ_ONLY_TOOLS, *web]
        allowed = [*tools, *mcp_allowed]
        dropped = [n for n in tr.allow_native if n not in tools and not n.startswith("mcp__")]
        if dropped:
            warnings.append(
                f"tools.allow: {', '.join(_dedupe(dropped))} ignored under access=read-only "
                "(only read, web and MCP tools are available)"
            )
    elif req.access is AccessLevel.WORKSPACE_WRITE:
        permission_mode = "acceptEdits"
        allowed = ["Bash", *web, *tr.allow_native, *mcp_allowed]
    else:
        permission_mode = "bypassPermissions"
        allowed = [*tr.allow_native, *mcp_allowed]

    system_prompt: str | SystemPromptPreset | None
    if req.instructions_mode == "replace":
        system_prompt = req.instructions
    else:
        preset: SystemPromptPreset = {"type": "preset", "preset": "claude_code"}
        if req.instructions:
            preset["append"] = req.instructions
        system_prompt = preset

    thinking: ThinkingConfig | None = None
    if req.thinking is True:
        thinking = {"type": "adaptive"}
    elif req.thinking is False:
        thinking = {"type": "disabled"}

    effort = req.effort
    if effort is not None:
        effort = provider.capabilities.effort_aliases.get(effort, effort)
        if effort not in provider.capabilities.effort_levels:
            warnings.append(f"effort {req.effort!r} is not a Claude effort level; ignored")
            effort = None

    overrides = dict(req.provider_options)
    env: dict[str, str] = {
        "CLAUDE_AGENT_SDK_CLIENT_APP": f"rayspec/{__version__}",
        **provider.settings_env,
        **_str_mapping(overrides.pop("env", None), "provider_options.env", warnings),
        **(run_env or {}),
        **req.env,
    }
    extra_mcp = overrides.pop("mcp_servers", None)
    mcp_servers: dict[str, Any] = {}
    if isinstance(extra_mcp, Mapping):
        mcp_servers.update({str(k): v for k, v in extra_mcp.items()})
    elif extra_mcp is not None:
        warnings.append("provider_options.mcp_servers: not a mapping; ignored")
    mcp_servers.update({s.name: _mcp_config(s) for s in req.mcp_servers})

    options = ClaudeAgentOptions(
        tools=tools,
        allowed_tools=_dedupe(allowed),
        disallowed_tools=_dedupe(disallowed),
        permission_mode=permission_mode,  # type: ignore[arg-type]
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        strict_mcp_config=bool(mcp_servers),
        model=req.model,
        effort=cast("Any", effort),
        thinking=thinking,
        max_turns=req.max_turns,
        max_budget_usd=req.budget_usd,
        output_format=(
            {"type": "json_schema", "schema": dict(req.output_schema)}
            if req.output_schema is not None
            else None
        ),
        resume=req.resume_session,
        fork_session=bool(req.resume_session and req.fork_session),
        cwd=req.cwd,
        cli_path=provider.cli_path,
        setting_sources=cast("Any", provider.setting_sources),
        include_partial_messages=True,
        env=env,
        stderr=stderr,
    )
    for key, value in overrides.items():
        if key in ADAPTER_OWNED_OPTIONS:
            warnings.append(f"provider_options.{key}: owned by the claude adapter; ignored")
        elif key in _OPTION_FIELDS:
            setattr(options, key, value)
        else:
            warnings.append(f"provider_options.{key}: not a ClaudeAgentOptions field; ignored")
    return options, replace(tr, warnings=tuple(warnings))


def _str_mapping(value: Any, where: str, warnings: list[str]) -> dict[str, str]:
    """Coerce a mapping-ish value to ``{str: str}``; a non-mapping appends a warning."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        warnings.append(f"{where}: not a mapping; ignored")
        return {}
    return {str(k): str(v) for k, v in value.items()}


# --------------------------------------------------------------------------------------------------
# Stream mapping
# --------------------------------------------------------------------------------------------------


def _flatten_content(content: str | list[dict[str, Any]] | None) -> str:
    """Tool-result content → text (text parts joined; other parts named by type)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            parts.append(str(part))
        elif part.get("type") == "text":
            parts.append(str(part.get("text", "")))
        else:
            parts.append(f"[{part.get('type', 'content')}]")
    return "\n".join(parts)


@dataclass(slots=True)
class _Streamed:
    """Which block kinds were already streamed as deltas for the in-flight assistant message."""

    message_id: str | None = None
    text: bool = False
    thinking: bool = False


@dataclass(slots=True)
class _StreamState:
    """Everything :class:`_Mapper` learns from the message stream (consumed by ``_finish``)."""

    session_id: str | None = None
    init_model: str | None = None
    last_model: str | None = None
    result: ResultMessage | None = None
    synthetic_error: str | None = None
    synthetic_text: str = ""
    last_text: str = ""
    streamed_text: str = ""
    tool_names: dict[str, str] = field(default_factory=dict)
    streamed: dict[str | None, _Streamed] = field(default_factory=dict)
    #: usage per completed assistant message (keyed by API message id) — Claude Code repeats
    #: a message's usage on every content-block message of that API message, so the last one wins
    message_usage: dict[str, Usage] = field(default_factory=dict)
    anonymous_usage: Usage = field(default_factory=Usage)

    @property
    def partial_usage(self) -> Usage:
        """Usage of every completed assistant message so far (what an interrupted attempt bills)."""
        total = self.anonymous_usage
        for usage in self.message_usage.values():
            total = total + usage
        return total


class _Mapper:
    """Maps SDK messages onto :class:`AgentEvent` and records result-relevant state."""

    def __init__(self, emit: EmitFn) -> None:
        self.emit = emit
        self.state = _StreamState()

    async def _send(self, event: AgentEvent) -> None:
        event.ts = _now()
        await self.emit(event)

    async def handle(self, message: Any) -> None:
        """Dispatch one SDK message (unknown kinds are ignored)."""
        if isinstance(message, StreamEvent):
            await self._stream_event(message)
        elif isinstance(message, AssistantMessage):
            await self._assistant(message)
        elif isinstance(message, UserMessage):
            await self._user(message)
        elif isinstance(message, ResultMessage):
            self.state.result = message
        elif isinstance(message, SystemMessage):
            await self._system(message)
        elif isinstance(message, RateLimitEvent):
            info = message.rate_limit_info
            text = f"rate limit {info.status}"
            if info.rate_limit_type:
                text += f" ({info.rate_limit_type})"
            if info.utilization is not None:
                text += f" utilization {info.utilization:.0%}"
            await self._send(AgentEvent(kind="warning", text=text, data=dict(info.raw)))

    async def _system(self, message: SystemMessage) -> None:
        data = dict(message.data)
        if message.subtype == "init":
            self.state.session_id = data.get("session_id") or self.state.session_id
            self.state.init_model = data.get("model")
            await self._send(
                AgentEvent(
                    kind="session",
                    text=str(self.state.session_id or ""),
                    data={
                        "session_id": self.state.session_id,
                        "model": data.get("model"),
                        "tools": list(data.get("tools") or []),
                        "cwd": data.get("cwd"),
                        "permission_mode": data.get("permission_mode"),
                    },
                )
            )
            return
        await self._send(AgentEvent(kind="raw", name=message.subtype, data=data))

    async def _stream_event(self, message: StreamEvent) -> None:
        ev = message.event
        nested = message.parent_tool_use_id is not None
        kind = ev.get("type")
        if kind == "message_start":
            msg = ev.get("message") or {}
            self.state.streamed[message.parent_tool_use_id] = _Streamed(message_id=msg.get("id"))
            return
        if kind != "content_block_delta":
            return
        tracker = self.state.streamed.setdefault(message.parent_tool_use_id, _Streamed())
        delta = ev.get("delta") or {}
        dkind = delta.get("type")
        if dkind == "text_delta":
            text = str(delta.get("text", ""))
            tracker.text = True
            if not nested:
                self.state.streamed_text += text
            await self._send(AgentEvent(kind="text_delta", text=text, nested=nested))
        elif dkind == "thinking_delta":
            tracker.thinking = True
            await self._send(
                AgentEvent(kind="reasoning", text=str(delta.get("thinking", "")), nested=nested)
            )

    async def _assistant(self, message: AssistantMessage) -> None:
        nested = message.parent_tool_use_id is not None
        texts = [b.text for b in message.content if isinstance(b, TextBlock)]
        if message.model == "<synthetic>" and message.error:
            self.state.synthetic_error = message.error
            self.state.synthetic_text = "\n".join(texts)
            await self._send(
                AgentEvent(
                    kind="error",
                    text=self.state.synthetic_text or message.error,
                    data={
                        "error": message.error,
                        "transient": message.error in TRANSIENT_SYNTHETIC_ERRORS,
                    },
                    nested=nested,
                )
            )
            return
        if message.model and message.model != "<synthetic>" and not nested:
            self.state.last_model = message.model
        await self._message_usage(message)
        tracker = self.state.streamed.pop(message.parent_tool_use_id, None)
        same_message = tracker is not None and (
            tracker.message_id is None
            or message.message_id is None
            or tracker.message_id == message.message_id
        )
        text_streamed = bool(tracker and same_message and tracker.text)
        thinking_streamed = bool(tracker and same_message and tracker.thinking)
        if not nested:
            if texts:
                self.state.last_text = "\n".join(texts)
            self.state.streamed_text = ""
        for block in message.content:
            if isinstance(block, TextBlock):
                if not text_streamed:
                    await self._send(AgentEvent(kind="text", text=block.text, nested=nested))
            elif isinstance(block, ThinkingBlock):
                if not thinking_streamed:
                    await self._send(
                        AgentEvent(kind="reasoning", text=block.thinking, nested=nested)
                    )
            elif isinstance(block, ToolUseBlock):
                self.state.tool_names[block.id] = block.name
                await self._send(
                    AgentEvent(
                        kind="tool_call",
                        name=block.name,
                        call_id=block.id,
                        data=dict(block.input),
                        nested=nested,
                    )
                )

    async def _message_usage(self, message: AssistantMessage) -> None:
        """Fold ``message.usage`` into the partial total and stream a ``usage`` event.

        ``data["usage"]`` is this API message's usage, ``data["turn_total"]`` the cumulative usage
        of every completed assistant message of the attempt — the neutral shape the engine reads
        when the attempt is interrupted before the ``ResultMessage`` arrives.
        """
        raw = message.usage
        if not isinstance(raw, Mapping) or not raw:
            return
        usage = _usage_from(raw)
        before = self.state.partial_usage
        if message.message_id:
            self.state.message_usage[str(message.message_id)] = usage
        else:
            self.state.anonymous_usage = self.state.anonymous_usage + usage
        total = self.state.partial_usage
        if total == before:
            return  # the same API message repeated (one content block per CLI message): no news
        await self._send(
            AgentEvent(
                kind="usage",
                data={
                    "usage": usage_dict(usage),
                    "turn_total": usage_dict(total),
                    "message_id": message.message_id,
                },
                nested=message.parent_tool_use_id is not None,
            )
        )

    async def _user(self, message: UserMessage) -> None:
        if isinstance(message.content, str):
            return
        nested = message.parent_tool_use_id is not None
        for block in message.content:
            if isinstance(block, ToolResultBlock):
                await self._send(
                    AgentEvent(
                        kind="tool_result",
                        name=self.state.tool_names.get(block.tool_use_id),
                        call_id=block.tool_use_id,
                        text=_flatten_content(block.content),
                        data={"is_error": bool(block.is_error)},
                        nested=nested,
                    )
                )


# --------------------------------------------------------------------------------------------------
# Result folding
# --------------------------------------------------------------------------------------------------


def _usage_from(raw: Mapping[str, Any] | None) -> Usage:
    """``ResultMessage.usage`` → :class:`Usage` (``input`` includes cache read + write)."""
    raw = raw or {}

    def num(key: str) -> int:
        value = raw.get(key)
        return int(value) if isinstance(value, int | float) else 0

    cache_read = num("cache_read_input_tokens")
    cache_write = num("cache_creation_input_tokens")
    return Usage(
        input=num("input_tokens") + cache_read + cache_write,
        cached_input=cache_read,
        cache_write=cache_write,
        output=num("output_tokens"),
    )


def _model_from_usage(model_usage: Mapping[str, Any] | None) -> str | None:
    """The model with the most output tokens in ``ResultMessage.model_usage``."""
    if not model_usage:
        return None
    best: tuple[int, str] | None = None
    for name, entry in model_usage.items():
        out = entry.get("outputTokens", 0) if isinstance(entry, Mapping) else 0
        out = int(out) if isinstance(out, int | float) else 0
        if best is None or out > best[0]:
            best = (out, str(name))
    return best[1] if best else None


def _status_from(result: ResultMessage) -> ResultStatus:
    reason = result.terminal_reason or ""
    if reason.startswith("aborted"):
        return "interrupted"
    if result.subtype == "error_max_turns":
        return "max_turns"
    if result.subtype == "error_max_budget_usd":
        return "budget"
    if result.is_error:
        return "error"
    return "success"


def _error_from(
    result: ResultMessage, state: _StreamState, status: ResultStatus
) -> AgentError | None:
    """Classify the error of a non-success result (transient vs fatal, kind, code)."""
    if status == "success":
        return None
    message = ""
    if result.errors:
        message = "; ".join(str(e) for e in result.errors if e)
    if not message and result.result:
        message = result.result
    if not message and state.synthetic_text:
        message = state.synthetic_text
    if status == "interrupted":
        return AgentError(
            kind="transport",
            message=message or f"interrupted ({result.terminal_reason})",
            transient=False,
            code=result.terminal_reason,
        )
    if status == "max_turns":
        return AgentError(
            kind="model",
            message=message or f"max_turns reached after {result.num_turns} turns",
            transient=False,
            code=result.subtype,
        )
    if status == "budget":
        return AgentError(
            kind="budget",
            message=message or f"max budget exceeded (${result.total_cost_usd or 0:.4f})",
            transient=False,
            code=result.subtype,
        )
    synthetic = state.synthetic_error
    http_status = result.api_error_status
    if synthetic in FATAL_SYNTHETIC_ERRORS:
        kind_map: dict[str, ErrorKind] = {
            "authentication_failed": "auth",
            "billing_error": "budget",
            "invalid_request": "api",
        }
        return AgentError(
            kind=kind_map[synthetic],
            message=message or synthetic,
            transient=False,
            code=http_status if http_status is not None else synthetic,
        )
    transient = (http_status in TRANSIENT_API_STATUSES) or (synthetic in TRANSIENT_SYNTHETIC_ERRORS)
    is_api = result.terminal_reason == "api_error" or http_status is not None or synthetic
    kind: ErrorKind = "api" if is_api else "unknown"
    code: str | int | None = (
        http_status if http_status is not None else (synthetic or result.subtype)
    )
    return AgentError(
        kind=kind,
        message=message or f"claude returned an error result ({result.subtype})",
        transient=bool(transient),
        code=code,
    )


def _result_from_error(exc: ResultError) -> ResultMessage:
    """Synthesize a :class:`ResultMessage` from a ``ResultError`` whose result was not yielded."""
    data = exc.data
    return ResultMessage(
        subtype=exc.subtype or "error_during_execution",
        duration_ms=int(data.get("duration_ms") or 0),
        duration_api_ms=int(data.get("duration_api_ms") or 0),
        is_error=True,
        num_turns=int(data.get("num_turns") or 0),
        session_id=exc.session_id or "",
        total_cost_usd=data.get("total_cost_usd"),
        usage=data.get("usage"),
        result=exc.result,
        errors=exc.errors or None,
        api_error_status=exc.api_error_status,
        terminal_reason=exc.terminal_reason,
    )


# --------------------------------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------------------------------


class ClaudeProvider:
    """Claude Agent SDK provider (``id="claude"``). One ``query()`` subprocess per request."""

    id: str = "claude"
    capabilities: ProviderCapabilities = CLAUDE_CAPABILITIES

    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        """``settings``: ``setting_sources``, ``cli_path``, ``env`` (see the module docstring)."""
        settings = dict(settings or {})
        # Read by the SDK from the *parent* process environment at connect() time: skips the
        # `claude -v` probe (2 s, per subprocess) that only logs a warning anyway.
        os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
        self.setting_sources: list[str] | None = _validate_setting_sources(
            settings.get("setting_sources", list(DEFAULT_SETTING_SOURCES))
        )
        cli_path = settings.get("cli_path")
        if cli_path is not None and not isinstance(cli_path, str | os.PathLike):
            raise _settings_error("cli_path", "must be a path to the claude binary")
        self.cli_path: str | None = str(cli_path) if cli_path else None
        raw_env = settings.get("env")
        if raw_env is not None and not isinstance(raw_env, Mapping):
            raise _settings_error("env", "must be a mapping of NAME: value")
        self.settings_env: dict[str, str] = {str(k): str(v) for k, v in (raw_env or {}).items()}
        self.run_id: str | None = None
        self.workdir: str | None = None
        self.run_env: dict[str, str] = {}
        self.max_parallel: int | None = None

    # -- lifecycle ------------------------------------------------------------------------------

    async def open(
        self, *, run_id: str, workdir: str, env: Mapping[str, str], max_parallel: int
    ) -> None:
        """Remember per-run context. ``env`` is merged under each request's ``env``."""
        self.run_id = run_id
        self.workdir = workdir
        self.run_env = dict(env)
        self.max_parallel = max_parallel

    async def aclose(self) -> None:
        """Nothing to release: every ``query()`` owns (and closes) its own subprocess."""
        self.run_id = None

    # -- run ------------------------------------------------------------------------------------

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        """Run one agent invocation; infrastructure failures raise :class:`ProviderError`.

        Every exception the SDK lets out of ``query()`` is mapped: ``CLINotFoundError`` →
        :class:`ProviderNotInstalledError`, ``CLIConnectionError`` → fatal transport error,
        ``ProcessError``/``CLIJSONDecodeError`` → transient transport error, other
        ``ClaudeSDKError`` → provider error, any other ``Exception`` (bare control-request
        timeouts, ``RuntimeError`` from resume materialization) → transient transport error. A
        ``ResultError`` is folded into the result (its payload is the CLI's real error result).
        Cancellation is never caught.
        """
        stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        options, translation = build_options(self, req, stderr_tail.append, run_env=self.run_env)
        mapper = _Mapper(emit)
        for warning in translation.warnings:
            await emit(AgentEvent(kind="warning", text=warning, ts=_now()))
        started = time.monotonic()
        timed_out = False
        try:
            with anyio.fail_after(req.timeout_s):
                async with contextlib.aclosing(_open_stream(req.prompt, options)) as stream:
                    async for message in stream:
                        await mapper.handle(message)
        except TimeoutError:
            timed_out = True
        except ResultError as exc:
            # the error ResultMessage is yielded first (captured by the mapper), then raised
            if mapper.state.result is None:
                mapper.state.result = _result_from_error(exc)
        except CLINotFoundError as exc:
            raise ProviderNotInstalledError(
                "Claude Code CLI not found", hint=f"{_INSTALL_HINT} ({_first_line(exc)})"
            ) from exc
        except CLIConnectionError as exc:
            raise ProviderError(
                f"cannot connect to Claude Code: {_first_line(exc)}",
                kind="transport",
                transient=False,
                hint=_stderr_hint(stderr_tail),
            ) from exc
        except (ProcessError, CLIJSONDecodeError) as exc:
            raise ProviderError(
                f"Claude Code process failed: {_first_line(exc)}",
                kind="transport",
                transient=True,
                hint=_stderr_hint(stderr_tail),
            ) from exc
        except ClaudeSDKError as exc:
            raise ProviderError(
                f"Claude Agent SDK error: {_first_line(exc)}",
                kind="provider",
                transient=False,
                hint=_stderr_hint(stderr_tail),
            ) from exc
        except Exception as exc:
            # the SDK also raises non-SDK exceptions out of query(): a bare Exception on a control
            # request timeout (initialize), RuntimeError from resume materialization, ...
            # (cancellation is BaseException-derived on both anyio backends and is not caught here)
            raise ProviderError(
                f"Claude Agent SDK failed: {_first_line(exc)}",
                kind="transport",
                transient=True,
                hint=_stderr_hint(stderr_tail),
            ) from exc
        duration_ms = int((time.monotonic() - started) * 1000)
        return await self._finish(
            req,
            mapper.state,
            emit,
            duration_ms=duration_ms,
            timed_out=timed_out,
            stderr_tail=stderr_tail,
        )

    async def _finish(
        self,
        req: AgentRequest,
        state: _StreamState,
        emit: EmitFn,
        *,
        duration_ms: int,
        timed_out: bool,
        stderr_tail: deque[str],
    ) -> AgentResult:
        partial_text = "\n".join(t for t in (state.last_text, state.streamed_text) if t)
        if timed_out and state.result is None:
            return AgentResult(
                status="timeout",
                text=partial_text,
                session_ref=state.session_id,
                duration_ms=duration_ms,
                model=state.last_model or state.init_model,
                error=AgentError(
                    kind="timeout",
                    message=f"timed out after {req.timeout_s}s",
                    transient=False,
                ),
                raw={"stderr_tail": list(stderr_tail)},
            )
        result = state.result
        if result is None:
            raise ProviderError(
                "Claude Code ended without a result message",
                kind="transport",
                transient=True,
                hint=_stderr_hint(stderr_tail),
            )
        status = _status_from(result)
        error = _error_from(result, state, status)
        text = result.result if result.result is not None else partial_text
        if status == "error" and (state.synthetic_error or result.terminal_reason == "api_error"):
            text = state.last_text  # the "API Error: ..." prose is not output; keep earlier text
        raw: dict[str, Any] = {
            "subtype": result.subtype,
            "terminal_reason": result.terminal_reason,
            "stop_reason": result.stop_reason,
            "api_error_status": result.api_error_status,
            "errors": list(result.errors or []),
            "sdk_duration_ms": result.duration_ms,
            "duration_api_ms": result.duration_api_ms,
            "model_usage": dict(result.model_usage or {}),
            "permission_denials": list(result.permission_denials or []),
            "stderr_tail": list(stderr_tail),
        }
        if timed_out:
            # the deadline fired during the SDK's (shielded) transport teardown, after the result
            # frame: the step finished, so fold the result and only record the slow close
            raw["teardown_timed_out"] = True
        for denial in result.permission_denials or []:
            event = _denial_event(denial)
            event.ts = _now()
            await emit(event)
        return AgentResult(
            status=status,
            text=text,
            structured=result.structured_output,
            session_ref=result.session_id or state.session_id,
            usage=_usage_from(result.usage),
            cost_usd=result.total_cost_usd,
            cost_source="provider" if result.total_cost_usd is not None else "none",
            duration_ms=duration_ms,
            num_turns=result.num_turns,
            model=_model_from_usage(result.model_usage) or state.last_model or state.init_model,
            error=error,
            denials=tuple(denial_of(d) for d in result.permission_denials or ()),
            raw=raw,
        )

    # -- health ---------------------------------------------------------------------------------

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
        """SDK version, CLI path/version (``claude -v``, 5 s per attempt, one retry on timeout —
        up to 10 s when the CLI hangs), auth; ``probe`` runs 1 turn.

        ``ok`` requires a CLI that is found AND reports a version (and a passing probe when
        requested). ``auth`` is ``"ok"`` when a credential env var is set or the ``claude`` CLI's
        own login is found (:func:`cli_login_source`: ``~/.claude/.credentials.json`` or the
        macOS keychain item — existence only), else ``"unknown"`` (``"missing"`` is never
        reported: a login may live where we do not look; the probe is the proof).
        """
        details: list[str] = [f"claude-agent-sdk {SDK_VERSION}"]
        if CLI_BUNDLED_VERSION:
            details.append(f"bundled CLI {CLI_BUNDLED_VERSION}")
        cli_path = self.cli_path or find_cli()
        cli_version: str | None = None
        if cli_path is None:
            details.append("claude CLI not found (bundled, PATH, known locations)")
        else:
            cli_version = await cli_version_of(
                cli_path, timeout_s=CLI_VERSION_TIMEOUT_S, retries=CLI_VERSION_RETRIES
            )
            if cli_version is None:
                details.append(f"`{cli_path} -v` did not report a version (not runnable?)")
        ok = cli_version is not None
        auth_var = next((v for v in AUTH_ENV_VARS if os.environ.get(v)), None)
        auth: Any = "unknown"
        if auth_var:
            auth = "ok"
            details.append(f"auth via {auth_var}")
        else:
            login = await to_thread.run_sync(cli_login_source)
            if login:
                auth = "ok"
                details.append(f"auth: {login}")
            else:
                details.append("auth: login state unknown")
        if probe and ok:
            probe_ok, note = await self._probe()
            ok = probe_ok
            details.append(f"probe: {note}")
        return ProviderHealth(
            ok=ok,
            sdk_version=SDK_VERSION,
            cli_path=cli_path,
            cli_version=cli_version,
            auth=auth,
            details=tuple(details),
        )

    async def _probe(self) -> tuple[bool, str]:
        options = ClaudeAgentOptions(
            tools=[],
            allowed_tools=[],
            max_turns=1,
            permission_mode="dontAsk",
            setting_sources=[],
            cli_path=self.cli_path,
            env={"CLAUDE_AGENT_SDK_CLIENT_APP": f"rayspec/{__version__}", **self.settings_env},
        )
        mapper = _Mapper(_drop)
        try:
            with anyio.fail_after(120):
                async with contextlib.aclosing(_open_stream(PROBE_PROMPT, options)) as s:
                    async for message in s:
                        await mapper.handle(message)
        except TimeoutError:
            return False, "timed out"
        except ResultError:
            pass
        except Exception as exc:  # a health probe reports, never raises
            return False, f"{type(exc).__name__}: {_first_line(exc)}"
        result = mapper.state.result
        if result is None:
            return False, "no result message"
        if result.is_error:
            return False, f"error result ({result.subtype}): {result.result or ''}".strip()
        text = (result.result or "").strip()
        return True, f"ok ({text[:40]!r}, {result.num_turns} turn)"


async def _drop(_event: AgentEvent) -> None:
    return None


def cli_config_dir() -> Path:
    """Where the ``claude`` CLI keeps its state: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _keychain_has_cli_login() -> bool:
    """macOS only: does the keychain hold the ``Claude Code-credentials`` item? The lookup asks
    for the item's existence (exit status), never for its secret (no ``-w``/``-g``), is bounded
    by :data:`KEYCHAIN_LOOKUP_TIMEOUT_S` and never raises (no ``security`` → ``False``)."""
    if platform.system() != "Darwin":
        return False
    security = shutil.which("security")
    if security is None:
        return False
    try:
        proc = subprocess.run(
            [security, "find-generic-password", "-s", CLI_LOGIN_KEYCHAIN_SERVICE],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_LOOKUP_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return proc.returncode == 0


def cli_login_source() -> str | None:
    """Evidence of the ``claude`` CLI's own (claude.ai) login, or ``None`` when there is none.

    Checks ``<config dir>/.credentials.json`` (Linux and every platform; ``CLAUDE_CONFIG_DIR``
    honoured), then on macOS the keychain item ``Claude Code-credentials``. Existence only: the
    file is never read and the keychain secret never requested, so the returned string names the
    *source* (``claude.ai login (~/.claude/.credentials.json)`` / ``claude.ai login (macOS
    keychain)``) and can be printed safely.
    """
    credentials = cli_config_dir() / ".credentials.json"
    if credentials.is_file():
        shown = str(credentials)
        home = str(Path.home())
        if shown.startswith(home + os.sep):
            shown = "~" + shown[len(home) :]
        return f"claude.ai login ({shown})"
    if _keychain_has_cli_login():
        return "claude.ai login (macOS keychain)"
    return None


def _settings_error(key: str, problem: str) -> ProviderError:
    return ProviderError(
        f"invalid providers.claude.{key}: {problem}",
        kind="provider",
        transient=False,
        hint=f"fix providers.claude.{key} in config.yaml",
    )


def _validate_setting_sources(raw: Any) -> list[str] | None:
    """``null`` → all sources; otherwise a list of ``user|project|local`` (else ProviderError)."""
    if raw is None:
        return None
    if isinstance(raw, str | bytes) or not isinstance(raw, Iterable):
        raise _settings_error(
            "setting_sources", "must be a list of user|project|local (or null for all)"
        )
    sources = [str(s) for s in raw]
    bad = [s for s in sources if s not in VALID_SETTING_SOURCES]
    if bad:
        raise _settings_error(
            "setting_sources", f"unknown source(s) {', '.join(bad)}; allowed: user|project|local"
        )
    return sources


#: Longest denial wording kept on a record: it is a human note, not data anything reads, and a
#: step may collect one per refused call.
DENIAL_REASON_MAX = 300


def _short(text: str) -> str:
    return text if len(text) <= DENIAL_REASON_MAX else text[: DENIAL_REASON_MAX - 1] + "\u2026"


def denial_of(denial: Any) -> Denial:
    """One Claude ``permission_denials`` entry as the neutral :class:`Denial`.

    The tool INPUT is deliberately dropped: it is step content (a command line, a file body) and
    the record is not the place for it. What the step needs to know is that ``Bash`` was refused.
    """
    if isinstance(denial, Mapping):
        name = denial.get("tool_name")
        call_id = denial.get("tool_use_id")
        return Denial(
            tool=str(name) if name else "unknown",
            reason=_short(str(denial.get("message") or "permission denied")),
            call_id=str(call_id) if call_id else None,
        )
    return Denial(tool="unknown", reason=_short(f"permission denied: {denial}"))


def _denial_event(denial: Any) -> AgentEvent:
    """The live ``warning`` event for one denial.

    It carries the tool INPUT, which :func:`denial_of` deliberately drops: an event is a live
    view of what the agent is doing and the console shows it as it happens, while a record is
    kept and read back later. What is streamed and what is persisted are not the same promise.
    """
    if isinstance(denial, Mapping):
        name = denial.get("tool_name")
        return AgentEvent(
            kind="warning",
            text=f"permission denied: {name}",
            name=str(name) if name else None,
            call_id=denial.get("tool_use_id"),
            data={"input": denial.get("tool_input")},
        )
    return AgentEvent(kind="warning", text=f"permission denied: {denial}")


def _first_line(exc: BaseException) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__


def _stderr_hint(tail: deque[str]) -> str | None:
    if not tail:
        return None
    return "claude stderr (last lines):\n" + "\n".join(tail)


# --------------------------------------------------------------------------------------------------
# CLI discovery (shared with `rayspec doctor`)
# --------------------------------------------------------------------------------------------------


def _cli_binary_name() -> str:
    return "claude.exe" if platform.system() == "Windows" else "claude"


def _bundled_dir() -> Path:
    return Path(claude_agent_sdk.__file__).parent / "_bundled"


def _bundled_cli_path() -> str | None:
    """The CLI shipped inside the ``claude_agent_sdk`` wheel, if present."""
    path = _bundled_dir() / _cli_binary_name()
    return str(path) if path.is_file() else None


def _known_cli_locations() -> tuple[Path, ...]:
    """The SDK's fallback install locations for the current platform."""
    if platform.system() == "Windows":
        return (Path.home() / ".local/bin/claude.exe",)
    return _KNOWN_CLI_LOCATIONS


def find_cli() -> str | None:
    """Locate ``claude`` the way the SDK does: bundled → ``PATH`` → known install locations.

    Uses ``claude.exe`` and the SDK's Windows location list on Windows.
    """
    bundled = _bundled_cli_path()
    if bundled:
        return bundled
    which = shutil.which(_cli_binary_name())
    if which:
        return which
    for location in _known_cli_locations():
        if location.is_file():
            return str(location)
    return None


#: Per-attempt timeout of the ``claude -v`` version probe (seconds).
CLI_VERSION_TIMEOUT_S: float = 5.0
#: Extra attempts of the version probe after a failed/timed-out one (None only when all fail).
CLI_VERSION_RETRIES: int = 1


async def cli_version_of(
    cli_path: str, *, timeout_s: float = CLI_VERSION_TIMEOUT_S, retries: int = CLI_VERSION_RETRIES
) -> str | None:
    """Run ``<cli> -v`` under ``timeout_s`` and return the ``x.y.z`` it prints.

    A timed-out attempt (or one that prints no version) is retried up to ``retries`` times — the
    probe was seen timing out under load — so the worst case is ``(retries + 1) * timeout_s``
    (10 s with the defaults). An ``OSError`` (ENOENT/EACCES) is permanent: ``None`` at once.
    ``None`` when no attempt reports a version.
    """
    for _attempt in range(max(0, retries) + 1):
        try:
            with anyio.fail_after(timeout_s):
                result = await anyio.run_process([cli_path, "-v"], check=False)
        except TimeoutError:
            continue
        except OSError:
            return None
        match = _VERSION_RE.search(result.stdout.decode(errors="replace"))
        if match:
            return match.group(1)
    return None


__all__ = [
    "ADAPTER_OWNED_OPTIONS",
    "CLI_BUNDLED_VERSION",
    "CLI_VERSION_RETRIES",
    "CLI_VERSION_TIMEOUT_S",
    "DEFAULT_SETTING_SOURCES",
    "MERGED_OPTIONS",
    "SDK_VERSION",
    "STDERR_TAIL_LINES",
    "TRANSIENT_API_STATUSES",
    "VALID_SETTING_SOURCES",
    "ClaudeProvider",
    "build_options",
    "cli_config_dir",
    "cli_login_source",
    "cli_version_of",
    "find_cli",
]
