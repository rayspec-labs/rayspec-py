# SPDX-License-Identifier: Apache-2.0
"""Declared capability tables for the builtin providers.

Boundary: pure data. Importing this module never loads an SDK, so the loader/validator and the
``rayspec providers`` command can reason about every provider without the adapters installed.
The tables are the single source of truth for the capability matrix in the plan (§3.1); the
adapters in :mod:`rayspec.providers.claude` / :mod:`rayspec.providers.codex` re-export them.
"""

from __future__ import annotations

from types import MappingProxyType

from rayspec.providers.base import (
    TOOL_GROUPS,
    AccessLevel,
    InstructionsMode,
    ProviderCapabilities,
)

_ALL_ACCESS = frozenset(AccessLevel)
_BOTH_INSTRUCTION_MODES: frozenset[InstructionsMode] = frozenset({"append", "replace"})

#: Claude Agent SDK (``claude-agent-sdk``): everything except images in v1.
CLAUDE_CAPABILITIES = ProviderCapabilities(
    structured_output="enforced",
    session_resume=True,
    session_fork=True,
    instructions_modes=_BOTH_INSTRUCTION_MODES,
    access_levels=_ALL_ACCESS,
    tool_groups=frozenset({"read", "edit", "shell", "web", "agent", "mcp"}),
    raw_tool_names=True,
    max_turns=True,
    budget_usd=True,
    cost_reporting=True,
    effort_levels=frozenset({"low", "medium", "high", "xhigh", "max"}),
    effort_aliases={"minimal": "low"},
    thinking=True,
    denial_reporting=True,  # result.permission_denials names every refused call
    mcp_servers=True,
    env_injection=True,
    images=False,
)

#: OpenAI Codex SDK (``openai-codex``): no per-tool policy beyond web search, no turn/budget caps,
#: no USD cost reporting (pricing table instead), images not in v1 (plan §3.1: "images later").
CODEX_CAPABILITIES = ProviderCapabilities(
    structured_output="enforced",
    session_resume=True,
    session_fork=True,
    instructions_modes=_BOTH_INSTRUCTION_MODES,
    access_levels=_ALL_ACCESS,
    tool_groups=frozenset({"web"}),
    raw_tool_names=False,
    max_turns=False,
    budget_usd=False,
    cost_reporting=False,
    # max/ultra exist on the gpt-5.6 family (model-dependent; the API rejects them elsewhere)
    effort_levels=frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}),
    effort_aliases={},
    thinking=False,
    # a refused command FAILS the turn (sandboxError), so a completed turn never carries a
    # denial: there is nothing for `on_denial` to grade
    denial_reporting=False,
    mcp_servers=True,
    env_injection=True,
    images=False,
)

#: Scripted test double / ``--dry-run`` provider: accepts everything so it can stand in for any
#: provider after real capability validation has happened.
STUB_CAPABILITIES = ProviderCapabilities(
    structured_output="enforced",
    session_resume=True,
    session_fork=True,
    instructions_modes=_BOTH_INSTRUCTION_MODES,
    access_levels=_ALL_ACCESS,
    tool_groups=TOOL_GROUPS,
    raw_tool_names=True,
    max_turns=True,
    budget_usd=True,
    cost_reporting=True,
    effort_levels=frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"}),
    effort_aliases={},
    thinking=True,
    denial_reporting=True,
    mcp_servers=True,
    env_injection=True,
    images=True,
)

#: Current Claude Code built-in tool names (raw ``claude:<Name>`` entries are checked against
#: this set; ``mcp__*`` names are never checked).
KNOWN_CLAUDE_TOOLS: frozenset[str] = frozenset(
    {
        "Agent",
        "AskUserQuestion",
        "Bash",
        "Edit",
        "EnterPlanMode",
        "ExitPlanMode",
        "Glob",
        "Grep",
        "ListMcpResourcesTool",
        "NotebookEdit",
        "Read",
        "ReadMcpResourceTool",
        "Skill",
        "SlashCommand",
        "TaskOutput",
        "TaskStop",
        "TodoWrite",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)

#: Legacy Claude Code tool names → current names (rewritten with a warning).
RENAMED_CLAUDE_TOOLS: MappingProxyType[str, str] = MappingProxyType(
    {
        "Task": "Agent",
        "MultiEdit": "Edit",
        "BashOutput": "TaskOutput",
        "KillShell": "TaskStop",
    }
)

#: Builtin provider id → declared capabilities (used by the registry and the CLI matrix).
BUILTIN_CAPABILITIES: MappingProxyType[str, ProviderCapabilities] = MappingProxyType(
    {
        "claude": CLAUDE_CAPABILITIES,
        "codex": CODEX_CAPABILITIES,
        "stub": STUB_CAPABILITIES,
    }
)

__all__ = [
    "BUILTIN_CAPABILITIES",
    "CLAUDE_CAPABILITIES",
    "CODEX_CAPABILITIES",
    "KNOWN_CLAUDE_TOOLS",
    "RENAMED_CLAUDE_TOOLS",
    "STUB_CAPABILITIES",
]
