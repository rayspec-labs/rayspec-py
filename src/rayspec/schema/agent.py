# SPDX-License-Identifier: Apache-2.0
"""Agent definitions: the reusable unit that carries provider/model/access/tool knobs."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from rayspec.schema.base import StrictModel
from rayspec.schema.common import AccessLevelName, EffortName, InstructionsModeName, Name

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
    max_turns: int | None = Field(default=None, ge=1)
    budget_usd: float | None = Field(default=None, gt=0)
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
]
