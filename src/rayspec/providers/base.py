# SPDX-License-Identifier: Apache-2.0
"""Neutral provider contract.

Rules for this module (mirrors Archon's ``types.ts`` discipline):

* **No SDK imports.** Nothing here may import ``claude_agent_sdk`` or ``openai_codex``.
* **No engine imports.** The engine depends on this module, never the other way round. The only
  rayspec import allowed here is :mod:`rayspec.errors` (the exception root).
* Everything the engine needs to know about a provider is data: its :class:`ProviderCapabilities`
  (declared statically on the :class:`ProviderRegistration`, so validation never loads an SDK) and
  the :class:`AgentRequest` → :class:`AgentEvent` stream → :class:`AgentResult` exchange.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, fields
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from rayspec.errors import RayspecError

# --------------------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------------------


class AccessLevel(StrEnum):
    """Neutral sandbox / permission level. Values are the YAML spellings."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    FULL = "full"


EffortLevel = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
StructuredMode = Literal["enforced", "best_effort", "none"]
InstructionsMode = Literal["append", "replace"]
ResultStatus = Literal["success", "error", "interrupted", "timeout", "max_turns", "budget"]
CostSource = Literal["provider", "table", "none"]
EventKind = Literal[
    "session",
    "text_delta",
    "text",
    "reasoning",
    "tool_call",
    "tool_result",
    "command_start",
    "command_output",
    "command_end",
    "file_change",
    "plan",
    "usage",
    "warning",
    "error",
    "raw",
]
#: ``stub_expectation`` is additive: a stub ``expect:`` block did not match the request the
#: engine built — an authoring-time assertion failure, never a provider/infrastructure problem.
ErrorKind = Literal[
    "api",
    "auth",
    "budget",
    "sandbox",
    "model",
    "transport",
    "timeout",
    "stub_expectation",
    "unknown",
]

#: Neutral tool groups understood by ``tools.allow`` / ``tools.deny``. ``mcp:<server>[/<tool>]`` is
#: also accepted (prefix match), and provider-native names may be passed as ``<provider>:<Name>``.
TOOL_GROUPS: frozenset[str] = frozenset({"read", "edit", "shell", "web", "agent", "mcp"})


# --------------------------------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider can honour. The validator maps YAML fields onto these flags.

    ``cost_reporting`` is informational only (the console shows the cost source); every other
    field gates a YAML feature and yields ``UnsupportedFeatureError`` when violated.
    """

    structured_output: StructuredMode
    session_resume: bool
    session_fork: bool
    instructions_modes: frozenset[InstructionsMode]
    access_levels: frozenset[AccessLevel]
    tool_groups: frozenset[str]
    raw_tool_names: bool
    max_turns: bool
    budget_usd: bool
    cost_reporting: bool
    effort_levels: frozenset[str]
    effort_aliases: Mapping[str, str] = field(default_factory=dict, hash=False, compare=True)
    thinking: bool = False
    mcp_servers: bool = False
    env_injection: bool = False
    images: bool = False
    extra: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        # keep the mapping immutable (frozen dataclass semantics); hash ignores it (hash=False)
        object.__setattr__(self, "effort_aliases", MappingProxyType(dict(self.effort_aliases)))

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable form (sets → sorted lists, mapping → dict)."""
        data: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, frozenset | set):
                data[f.name] = sorted(str(v) for v in value)
            elif isinstance(value, Mapping):
                data[f.name] = dict(value)
            else:
                data[f.name] = value
        return data


# --------------------------------------------------------------------------------------------------
# Request
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Allow/deny lists in the neutral vocabulary (groups, ``mcp:…``, or ``<provider>:<Name>``)."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """Neutral MCP server config (per agent)."""

    name: str
    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class AgentRequest:
    """One agent invocation (= one ``prompt:`` step attempt)."""

    step_path: str
    prompt: str
    cwd: str
    access: AccessLevel = AccessLevel.WORKSPACE_WRITE
    instructions: str | None = None
    instructions_mode: InstructionsMode = "append"
    model: str | None = None
    effort: str | None = None
    tools: ToolPolicy = field(default_factory=ToolPolicy)
    env: Mapping[str, str] = field(default_factory=dict)
    max_turns: int | None = None
    budget_usd: float | None = None
    thinking: bool | None = None
    output_schema: Mapping[str, Any] | None = None
    resume_session: str | None = None
    fork_session: bool = False
    mcp_servers: tuple[McpServerSpec, ...] = ()
    timeout_s: float | None = None
    provider_options: Mapping[str, Any] = field(default_factory=dict)
    run_id: str = ""
    step_attempt: int = 1


# --------------------------------------------------------------------------------------------------
# Streamed events
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class AgentEvent:
    """Provider-neutral streamed event. ``data`` carries provider specifics (args, exit codes…).

    ``ts`` is ``time.time()`` seconds; ``0.0`` means "unset" and the recorder stamps the current
    time.
    """

    kind: EventKind
    text: str = ""
    name: str | None = None
    call_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    nested: bool = False
    ts: float = 0.0


EmitFn = Callable[[AgentEvent], Awaitable[None]]


# --------------------------------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage. ``input`` includes cached/cache-write tokens; ``output`` includes reasoning."""

    input: int = 0
    cached_input: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input=self.input + other.input,
            cached_input=self.cached_input + other.cached_input,
            cache_write=self.cache_write + other.cache_write,
            output=self.output + other.output,
            reasoning=self.reasoning + other.reasoning,
        )


@dataclass(frozen=True, slots=True)
class Denial:
    """One tool call the provider's permission or sandbox layer refused.

    Additive to the provider contract. A denial is not an error — the turn may well have
    finished successfully — but it means the agent could not do something it tried to do, and a
    permission denial that only appears in a log is a silent failure. The engine records these
    on the step (``StepRecord.denials``) and, for an agent with ``on_denial: fail``, fails the
    step. ``tool`` is the provider's name for what was refused (``Bash``, ``shell``);
    ``reason`` is its own wording, ``call_id`` ties it to the ``tool_call`` event when known.
    """

    tool: str
    reason: str = ""
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentError:
    kind: ErrorKind
    message: str
    transient: bool
    code: str | int | None = None


@dataclass(slots=True)
class AgentResult:
    """Outcome of one agent invocation. Infrastructure failures raise :class:`ProviderError`."""

    status: ResultStatus
    text: str
    structured: Any | None = None
    session_ref: str | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None
    cost_source: CostSource = "none"
    duration_ms: int = 0
    num_turns: int | None = None
    model: str | None = None
    error: AgentError | None = None
    #: additive: the tool calls this turn had refused (permission or sandbox). Empty for a turn
    #: that was allowed everything it asked for.
    denials: tuple[Denial, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------------------
# Errors & health
# --------------------------------------------------------------------------------------------------


class ProviderError(RayspecError):
    """Infrastructure-level provider failure (CLI missing, transport died, auth…)."""

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        kind: str = "provider",
        hint: str | None = None,
    ):
        super().__init__(message, hint=hint)
        self.transient = transient
        self.kind = kind


class ProviderNotInstalledError(ProviderError):
    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message, transient=False, kind="not_installed", hint=hint)


class ProviderAuthError(ProviderError):
    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message, transient=False, kind="auth", hint=hint)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    ok: bool
    sdk_version: str | None = None
    cli_path: str | None = None
    cli_version: str | None = None
    auth: Literal["ok", "missing", "unknown"] = "unknown"
    details: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------------------
# Protocol & registration
# --------------------------------------------------------------------------------------------------


@runtime_checkable
class Provider(Protocol):
    """A live provider instance (one per run)."""

    id: str
    capabilities: ProviderCapabilities

    async def open(
        self, *, run_id: str, workdir: str, env: Mapping[str, str], max_parallel: int
    ) -> None:
        """Acquire per-run resources (e.g. the Codex app-server client pool keyed by env)."""
        ...

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult: ...

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth: ...

    async def aclose(self) -> None: ...


ProviderFactory = Callable[[Mapping[str, Any]], Provider]


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    """Registry entry. ``capabilities`` must be available WITHOUT importing the SDK."""

    id: str
    display_name: str
    capabilities: ProviderCapabilities
    factory: ProviderFactory


__all__ = [
    "TOOL_GROUPS",
    "AccessLevel",
    "AgentError",
    "AgentEvent",
    "AgentRequest",
    "AgentResult",
    "CostSource",
    "Denial",
    "EffortLevel",
    "EmitFn",
    "ErrorKind",
    "EventKind",
    "InstructionsMode",
    "McpServerSpec",
    "Provider",
    "ProviderAuthError",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderFactory",
    "ProviderHealth",
    "ProviderNotInstalledError",
    "ProviderRegistration",
    "ResultStatus",
    "StructuredMode",
    "ToolPolicy",
    "Usage",
]
