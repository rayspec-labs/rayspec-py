"""Declared capability tables must match the design plan (§3.1 capability table)."""

from __future__ import annotations

from rayspec.providers.base import TOOL_GROUPS, AccessLevel, ProviderCapabilities
from rayspec.providers.capabilities import (
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    KNOWN_CLAUDE_TOOLS,
    RENAMED_CLAUDE_TOOLS,
    STUB_CAPABILITIES,
)

ALL_ACCESS = frozenset(AccessLevel)


def test_claude_capabilities_match_plan():
    c = CLAUDE_CAPABILITIES
    assert isinstance(c, ProviderCapabilities)
    assert c.structured_output == "enforced"
    assert c.session_resume is True and c.session_fork is True
    assert c.instructions_modes == frozenset({"append", "replace"})
    assert c.access_levels == ALL_ACCESS
    assert c.tool_groups == frozenset({"read", "edit", "shell", "web", "agent", "mcp"})
    assert c.raw_tool_names is True
    assert c.max_turns is True and c.budget_usd is True and c.cost_reporting is True
    assert c.effort_levels == frozenset({"low", "medium", "high", "xhigh", "max"})
    assert dict(c.effort_aliases) == {"minimal": "low"}
    assert c.thinking is True and c.mcp_servers is True and c.env_injection is True
    assert c.images is False


def test_codex_capabilities_match_plan():
    c = CODEX_CAPABILITIES
    assert c.structured_output == "enforced"
    assert c.session_resume is True and c.session_fork is True
    assert c.instructions_modes == frozenset({"append", "replace"})
    assert c.access_levels == ALL_ACCESS
    assert c.tool_groups == frozenset({"web"})
    assert c.raw_tool_names is False
    assert c.max_turns is False and c.budget_usd is False and c.cost_reporting is False
    assert c.effort_levels == frozenset(
        {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    )
    assert dict(c.effort_aliases) == {}  # max/ultra pass through on the gpt-5.6 family
    assert c.thinking is False
    assert c.mcp_servers is True and c.env_injection is True
    assert c.images is False  # plan §3.1: Codex "images later" (not in v1)


def test_stub_capabilities_everything_on():
    c = STUB_CAPABILITIES
    assert c.structured_output == "enforced"
    assert c.session_resume and c.session_fork
    assert c.instructions_modes == frozenset({"append", "replace"})
    assert c.access_levels == ALL_ACCESS
    assert c.tool_groups == TOOL_GROUPS
    assert c.raw_tool_names and c.max_turns and c.budget_usd and c.cost_reporting
    assert c.effort_levels == frozenset(
        {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
    )
    assert dict(c.effort_aliases) == {}
    assert c.thinking and c.mcp_servers and c.env_injection and c.images


def test_effort_aliases_never_overlap_levels():
    for caps in (CLAUDE_CAPABILITIES, CODEX_CAPABILITIES, STUB_CAPABILITIES):
        for alias, target in caps.effort_aliases.items():
            assert alias not in caps.effort_levels
            assert target in caps.effort_levels


def test_claude_tool_name_tables():
    assert {"Read", "Glob", "Grep", "Edit", "Write", "Bash", "WebFetch", "WebSearch", "Agent"} <= (
        KNOWN_CLAUDE_TOOLS
    )
    assert RENAMED_CLAUDE_TOOLS == {
        "Task": "Agent",
        "MultiEdit": "Edit",
        "BashOutput": "TaskOutput",
        "KillShell": "TaskStop",
    }
    # every renamed target is a known current name; no legacy name is also "known"
    assert set(RENAMED_CLAUDE_TOOLS.values()) <= KNOWN_CLAUDE_TOOLS
    assert not (set(RENAMED_CLAUDE_TOOLS) & KNOWN_CLAUDE_TOOLS)


def test_capabilities_are_json_serialisable():
    import json

    for caps in (CLAUDE_CAPABILITIES, CODEX_CAPABILITIES, STUB_CAPABILITIES):
        json.dumps(caps.to_dict())
