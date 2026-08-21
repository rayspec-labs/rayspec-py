"""Neutral tool vocabulary → provider-native names (rayspec.providers._tools)."""

from __future__ import annotations

import pytest

from rayspec.providers._tools import (
    CLAUDE_GROUP_TOOLS,
    ToolTranslation,
    parse_tool_entry,
    translate_tools,
    validate_tools,
)
from rayspec.providers.base import AccessLevel, ToolPolicy
from rayspec.providers.capabilities import (
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    STUB_CAPABILITIES,
)
from rayspec.schema import ToolsSpec

# -- parsing ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "kind", "attrs"),
    [
        ("read", "group", {"group": "read"}),
        ("mcp", "group", {"group": "mcp"}),
        ("mcp:github", "mcp", {"server": "github", "tool": None}),
        ("mcp:jira/create_issue", "mcp", {"server": "jira", "tool": "create_issue"}),
        ("claude:WebFetch", "raw", {"provider": "claude", "name": "WebFetch"}),
        ("codex:anything", "raw", {"provider": "codex", "name": "anything"}),
        ("frobnicate", "invalid", {}),
        ("", "invalid", {}),
        ("mcp:", "invalid", {}),
        ("claude:", "invalid", {}),
        (":Read", "invalid", {}),
        ("mcp:a/b/c", "invalid", {}),
    ],
)
def test_parse_tool_entry(entry, kind, attrs):
    parsed = parse_tool_entry(entry)
    assert parsed.kind == kind
    for key, value in attrs.items():
        assert getattr(parsed, key) == value
    assert parsed.raw == entry


# -- claude -----------------------------------------------------------------------------------


def test_claude_group_table_covers_every_group():
    assert set(CLAUDE_GROUP_TOOLS) == {"read", "edit", "shell", "web", "agent", "mcp"}
    assert CLAUDE_GROUP_TOOLS["read"] == ("Read", "Glob", "Grep")
    assert CLAUDE_GROUP_TOOLS["edit"] == ("Edit", "Write", "NotebookEdit")
    assert CLAUDE_GROUP_TOOLS["shell"] == ("Bash",)
    assert CLAUDE_GROUP_TOOLS["web"] == ("WebFetch", "WebSearch")
    assert CLAUDE_GROUP_TOOLS["agent"] == ("Agent",)


def test_claude_groups_and_mcp_translate_to_native_names():
    t = translate_tools(
        ["read", "shell", "read"],
        ["web", "mcp:github", "mcp:jira/create_issue", "mcp"],
        "claude",
        CLAUDE_CAPABILITIES,
    )
    assert isinstance(t, ToolTranslation)
    assert t.allow_native == ("Read", "Glob", "Grep", "Bash")  # deduped, order kept
    # Claude Code permission grammar: mcp__<server> (whole server) / mcp__<server>__<tool>;
    # there is no wildcard form, and the bare `mcp` group becomes a flag the adapter expands
    # against req.mcp_servers.
    assert t.deny_native == ("WebFetch", "WebSearch", "mcp__github", "mcp__jira__create_issue")
    assert t.config_overrides == {}
    assert t.warnings == () and t.errors == ()
    assert t.ok
    assert t.deny_all_mcp is True and t.allow_all_mcp is False
    assert not any("*" in name for name in t.allow_native + t.deny_native)


def test_claude_bare_mcp_group_sets_flags_only():
    t = translate_tools(["mcp"], [], "claude", CLAUDE_CAPABILITIES)
    assert t.allow_native == () and t.allow_all_mcp is True and t.deny_all_mcp is False
    assert CLAUDE_GROUP_TOOLS["mcp"] == ()
    t2 = translate_tools(["mcp"], ["mcp"], "stub", STUB_CAPABILITIES)
    assert t2.allow_native == ("mcp",) and t2.deny_native == ("mcp",)
    assert t2.allow_all_mcp is True and t2.deny_all_mcp is True


def test_claude_raw_names_pass_through_with_known_check_and_renames():
    t = translate_tools(
        ["claude:WebFetch", "claude:Task", "claude:Frobnicate", "claude:mcp__x__y"],
        ["claude:MultiEdit"],
        "claude",
        CLAUDE_CAPABILITIES,
    )
    assert t.allow_native == ("WebFetch", "Agent", "Frobnicate", "mcp__x__y")
    assert t.deny_native == ("Edit",)
    assert t.errors == ()
    joined = "\n".join(t.warnings)
    assert "Task" in joined and "Agent" in joined  # renamed with a warning
    assert "MultiEdit" in joined and "Edit" in joined
    assert "Frobnicate" in joined and "unknown" in joined
    assert "mcp__x__y" not in joined  # mcp__* names are never checked
    assert len(t.warnings) == 3


def test_raw_names_for_another_provider_are_ignored_silently():
    t = translate_tools(["codex:Foo", "read"], ["stub:Bar"], "claude", CLAUDE_CAPABILITIES)
    assert t.allow_native == ("Read", "Glob", "Grep")
    assert t.deny_native == ()
    assert t.warnings == () and t.errors == ()


def test_unknown_entries_are_errors():
    t = translate_tools(["frobnicate", "mcp:"], [""], "claude", CLAUDE_CAPABILITIES)
    assert t.allow_native == () and t.deny_native == ()
    assert len(t.errors) == 3
    assert "frobnicate" in t.errors[0]
    assert not t.ok


# -- codex ------------------------------------------------------------------------------------


def test_codex_web_deny_becomes_config_override():
    t = translate_tools([], ["web"], "codex", CODEX_CAPABILITIES)
    assert t.config_overrides == {"web_search": "disabled"}
    assert t.allow_native == () and t.deny_native == ()
    assert t.errors == () and t.warnings == ()


def test_codex_web_allow_is_a_noop():
    t = translate_tools(["web"], [], "codex", CODEX_CAPABILITIES)
    assert t.config_overrides == {} and t.errors == ()


def test_codex_unsupported_groups_and_raw_names_are_errors_naming_the_capability():
    t = translate_tools(
        ["read", "codex:Foo", "mcp:github"], ["edit", "web"], "codex", CODEX_CAPABILITIES
    )
    assert t.config_overrides == {"web_search": "disabled"}
    assert len(t.errors) == 4
    by_text = "\n".join(t.errors)
    assert "tool_groups" in by_text and "'codex'" in by_text
    assert "raw_tool_names" in by_text and "codex:Foo" in by_text
    assert "'read'" in by_text and "'edit'" in by_text and "mcp:github" in by_text
    assert not t.ok


def test_codex_ignores_claude_raw_names():
    t = translate_tools(["claude:WebFetch"], [], "codex", CODEX_CAPABILITIES)
    assert t.errors == () and t.allow_native == ()


# -- stub / generic ---------------------------------------------------------------------------


def test_stub_accepts_everything_and_keeps_neutral_names():
    t = translate_tools(
        ["read", "mcp:gh", "stub:Foo", "mcp:gh/issue"], ["edit"], "stub", STUB_CAPABILITIES
    )
    assert t.allow_native == ("read", "mcp:gh", "Foo", "mcp:gh/issue")
    assert t.deny_native == ("edit",)
    assert t.errors == () and t.warnings == ()


def test_accepts_tuples_and_tool_policy_inputs():
    policy = ToolPolicy(allow=("read",), deny=("shell",))
    t = translate_tools(policy.allow, policy.deny, "claude", CLAUDE_CAPABILITIES)
    assert t.allow_native == ("Read", "Glob", "Grep") and t.deny_native == ("Bash",)


def test_translate_tools_spec_form_is_accepted():
    """``translate_tools(ToolsSpec | ToolPolicy, provider_id[, capabilities])`` also works."""
    policy = ToolPolicy(allow=("read",), deny=("shell",))
    t = translate_tools(policy, "claude", capabilities=CLAUDE_CAPABILITIES)
    assert t.allow_native == ("Read", "Glob", "Grep") and t.deny_native == ("Bash",)
    # capabilities default to the registry's declared table for the provider id
    t2 = translate_tools(ToolsSpec(allow=["read"], deny=["web"]), "codex")
    assert t2.config_overrides == {"web_search": "disabled"} and len(t2.errors) == 1
    t3 = translate_tools(ToolsSpec(deny=["web"]), "claude")
    assert t3.deny_native == ("WebFetch", "WebSearch")
    t4 = translate_tools(ToolsSpec(deny=["web"]), "claude", capabilities=CLAUDE_CAPABILITIES)
    assert t4 == t3
    with pytest.raises(TypeError):
        translate_tools(ToolsSpec(), CLAUDE_CAPABILITIES)  # type: ignore[call-overload]


# -- validate_tools (loader helper) ----------------------------------------------------------


def test_validate_tools_returns_translation_errors():
    spec = ToolsSpec(allow=["read"], deny=["edit"])
    errors = validate_tools(spec, "codex", CODEX_CAPABILITIES)
    assert len(errors) == 2
    assert validate_tools(spec, "claude", CLAUDE_CAPABILITIES) == []
    assert validate_tools(ToolPolicy(allow=("web",)), "codex", CODEX_CAPABILITIES) == []


def test_validate_tools_read_only_access_cannot_allow_edit_or_shell():
    spec = ToolsSpec(allow=["edit", "shell", "read"])
    errors = validate_tools(spec, "claude", CLAUDE_CAPABILITIES, access=AccessLevel.READ_ONLY)
    assert len(errors) == 2
    assert "read-only" in errors[0] and "edit" in errors[0]
    assert "shell" in errors[1]
    assert validate_tools(spec, "claude", CLAUDE_CAPABILITIES, access="workspace-write") == []
    # raw shell tools count too
    errs = validate_tools(
        ToolsSpec(allow=["claude:Bash"]), "claude", CLAUDE_CAPABILITIES, access="read-only"
    )
    assert len(errs) == 1 and "Bash" in errs[0]


def test_validate_tools_unknown_access_string_is_an_error_message_not_an_exception():
    spec = ToolsSpec(allow=["read"])
    errors = validate_tools(spec, "claude", CLAUDE_CAPABILITIES, access="root")
    assert len(errors) == 1 and "root" in errors[0] and "read-only" in errors[0]


def test_validate_tools_flags_unknown_provider_prefix_when_known_set_given():
    spec = ToolsSpec(allow=["claud:Read"])
    assert validate_tools(spec, "claude", CLAUDE_CAPABILITIES) == []
    errors = validate_tools(
        spec, "claude", CLAUDE_CAPABILITIES, known_providers={"claude", "codex", "stub"}
    )
    assert len(errors) == 1 and "claud" in errors[0] and "claude" in errors[0]
