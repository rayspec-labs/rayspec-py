# SPDX-License-Identifier: Apache-2.0
"""Agent definitions: the reusable unit that carries provider/model/access/tool knobs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator

from rayspec.schema.base import StrictModel
from rayspec.schema.common import (
    AccessLevelName,
    EffortName,
    InstructionsModeName,
    Name,
    input_ref_name,
    parse_money,
)


def parse_templated_money(value: object) -> float | str:
    """``budget_usd``: a positive USD literal (``1.5``, ``"$1.50"``) OR exactly one
    ``{{ inputs.<name> }}`` reference, kept verbatim for the loader to resolve per run."""
    if input_ref_name(value) is not None:
        return value  # type: ignore[return-value]  # a str, narrowed by input_ref_name
    return parse_money(value)  # a literal → float, > 0 enforced


def parse_templated_turns(value: object) -> int | str:
    """``max_turns``: a positive integer literal OR exactly one ``{{ inputs.<name> }}`` reference.
    Unlike a token count there are no ``500k`` string forms — a string is a reference or nothing."""
    if input_ref_name(value) is not None:
        return value  # type: ignore[return-value]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"invalid max_turns {value!r}: use a positive integer or exactly {{{{ inputs.<name> }}}}"
        )
    if value < 1:
        raise ValueError("must be >= 1")
    return value


#: ``budget_usd`` on an agent: a literal USD amount or exactly ``{{ inputs.<name> }}`` (E1).
TemplatedMoney = Annotated[float | str, BeforeValidator(parse_templated_money)]
#: ``max_turns`` on an agent: a literal positive int or exactly ``{{ inputs.<name> }}`` (E1).
TemplatedTurns = Annotated[int | str, BeforeValidator(parse_templated_turns)]

#: ``network:`` — whether the agent may reach the network through its provider's own tools.
NetworkModeName = Literal["on", "off"]

#: What a refused tool call does to the step (agent-level, see :attr:`AgentDef.on_denial`).
DenialPolicy = Literal["warn", "fail"]


class ToolsSpec(StrictModel):
    """Neutral allow/deny lists.

    Entries are tool groups (``read edit shell web agent``), ``mcp:<server>[/<tool>]``, or a
    provider-native name prefixed with the provider id (``claude:WebFetch``).
    """

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class CommandsSpec(StrictModel):
    """``commands:`` — which shell commands an agent may run, as regular expressions.

    Both lists are Python regular expressions matched against the command line a provider is
    about to run; ``deny`` is checked first, and a non-empty ``allow`` means "nothing else".
    They are validated (and compiled) at load time so a broken pattern is a file-and-line error
    rather than a control that quietly matches nothing.

    Enforcement needs the provider to hand rayspec its tool calls before they run. A provider
    that can do that declares ``command_policy`` in its capabilities; on every other provider
    ``rayspec validate`` warns that the block is advisory. See ``docs/policy.md``.
    """

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    @classmethod
    def _what(cls) -> str:
        return "commands policy"

    @field_validator("allow", "deny")
    @classmethod
    def _compilable(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"{pattern!r} is not a valid regular expression: {exc}") from None
        return value


class McpServerDef(StrictModel):
    """An MCP server an agent may use (mirrors ``providers.base.McpServerSpec``)."""

    transport: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def _what(cls) -> str:
        return "mcp server"

    @model_validator(mode="after")
    def _transport_fields(self) -> McpServerDef:
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP servers need 'command'")
        if self.transport in {"http", "sse"} and not self.url:
            raise ValueError(f"{self.transport} MCP servers need 'url'")
        return self


class AgentDef(StrictModel):
    """A (possibly partial) agent definition. Unset fields are filled by merge/tier resolution."""

    provider: str | None = None
    model: str | None = None
    effort: EffortName | None = None
    access: AccessLevelName = "workspace-write"
    instructions: str | None = None
    instructions_file: str | None = None
    instructions_mode: InstructionsModeName = "append"
    # `ge`/`gt` cannot live on the Field (the value may be a `{{ inputs.x }}` string); the
    # numeric bound is enforced inside the validator for a literal instead.
    max_turns: TemplatedTurns | None = None
    budget_usd: TemplatedMoney | None = None
    tools: ToolsSpec = Field(default_factory=ToolsSpec)
    network: NetworkModeName | None = None
    commands: CommandsSpec | None = None
    thinking: bool | None = None
    #: What a refused tool call does to the step. ``warn`` (the default) records the denials on
    #: the step record and lets it stand; ``fail`` fails the step. An agent that is denied a
    #: tool did not do what it was asked, and on an unattended run that must be able to be loud.
    on_denial: DenialPolicy = "warn"
    mcp: dict[Name, McpServerDef] = Field(default_factory=dict)
    provider_options: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @classmethod
    def _what(cls) -> str:
        return "agent"

    @model_validator(mode="after")
    def _instructions_xor_file(self) -> AgentDef:
        if self.instructions is not None and self.instructions_file is not None:
            raise ValueError("set either 'instructions' or 'instructions_file', not both")
        return self


class AgentOverride(AgentDef):
    """``agent: {extends: <name>, ...}`` — only explicitly set fields override the base agent."""

    extends: str

    @classmethod
    def _what(cls) -> str:
        return "agent override"


def provider_option_block(provider: str, options: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The block one provider adapter reads out of a ``provider_options`` value.

    ``provider_options:`` is keyed by provider id and the engine narrows it to the running
    provider's block before an adapter sees it — but a request built by hand may carry either
    shape, so both are understood: a block whose only key is the provider id and whose value is a
    mapping unwraps to that inner mapping; anything else is the block itself.

    It lives next to the field, in the one package the adapters and :mod:`rayspec.policy` both
    import, because those two MUST narrow a block identically. A check that walks one shape while
    an adapter accepts two leaves the shape the check does not walk as an unguarded pass-through:
    that is exactly how ``provider_options.codex.codex.config`` reached a thread unexamined. One
    function, and no room for a nesting variant to diverge.
    """
    if not options:
        return {}
    inner = options.get(provider)
    if len(options) == 1 and isinstance(inner, Mapping):
        return inner
    return options


def parse_agent_def(data: Any, *, source: str | None = None) -> AgentDef:
    return AgentDef.parse(data, source=source)


__all__ = [
    "AgentDef",
    "AgentOverride",
    "CommandsSpec",
    "DenialPolicy",
    "McpServerDef",
    "NetworkModeName",
    "ToolsSpec",
    "parse_agent_def",
    "provider_option_block",
]
