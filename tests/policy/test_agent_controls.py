"""The neutral agent controls: ``network:`` and the ``commands:`` policy block."""

from __future__ import annotations

import pytest

from rayspec.errors import RayspecError
from rayspec.schema import SchemaError
from rayspec.schema.agent import AgentDef

from .conftest import Tree, validated

NETWORK_WF = """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: {provider}
      network: {mode}
    prompt: hello
"""


def test_network_off_denies_the_providers_web_tools(tree: Tree) -> None:
    rw, report = validated(tree, NETWORK_WF.format(provider="claude", mode="off"))
    assert report.ok
    assert "web" in rw.agent_for("think").tools.deny


def test_network_on_changes_nothing(tree: Tree) -> None:
    rw, report = validated(tree, NETWORK_WF.format(provider="claude", mode="on"))
    assert report.ok
    assert rw.agent_for("think").tools.deny == []
    assert report.warnings == []


def test_network_off_is_advisory_where_the_provider_has_no_web_tool_group(tree: Tree) -> None:
    caps = {"nowebs": _caps_without_web()}
    rw, report = validated(
        tree,
        NETWORK_WF.format(provider="nowebs", mode="off"),
        capabilities_for=caps.get,
    )
    joined = "\n".join(report.warnings)
    assert "network: off" in joined
    assert "advisory" in joined
    assert "web" not in rw.agent_for("think").tools.deny


def test_network_off_and_an_explicit_web_allow_contradict(tree: Tree) -> None:
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      network: off
      tools:
        allow: [web]
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    assert "network: off" in joined
    assert "tools.allow" in joined


COMMANDS_WF = """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      commands:
        deny: ['^\\s*rm\\s+-rf\\s+/']
    prompt: hello
"""


def test_commands_are_advisory_on_a_provider_that_cannot_filter_tool_calls(tree: Tree) -> None:
    _, report = validated(tree, COMMANDS_WF)
    joined = "\n".join(report.warnings)
    assert "commands:" in joined
    assert "advisory" in joined
    assert "claude" in joined
    assert report.ok


def test_commands_are_not_advisory_where_the_provider_declares_enforcement(tree: Tree) -> None:
    caps = {"filters": _caps_with_command_policy()}
    _, report = validated(
        tree,
        COMMANDS_WF.replace("provider: claude", "provider: filters"),
        capabilities_for=caps.get,
    )
    assert report.warnings == []


def test_a_broken_command_regex_is_a_load_error() -> None:
    with pytest.raises(SchemaError) as excinfo:
        AgentDef.parse({"provider": "claude", "commands": {"deny": ["([a-z"]}}, source="a.yaml")
    assert "not a valid regular expression" in str(excinfo.value)


def test_a_broken_command_regex_in_a_workflow_names_the_file_and_line(tree: Tree) -> None:
    tree.workflow(
        "wf",
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      commands:
        deny: ['([a-z']
    prompt: hello
""",
    )
    from rayspec.loader import load_workflow

    with pytest.raises(RayspecError) as excinfo:
        load_workflow("wf", project_root=tree.root, home=tree.home)
    text = str(excinfo.value)
    assert "not a valid regular expression" in text
    assert "workflows/wf.yaml:8" in text


def _caps_without_web():
    import dataclasses

    from rayspec.providers.capabilities import CLAUDE_CAPABILITIES

    return dataclasses.replace(
        CLAUDE_CAPABILITIES, tool_groups=frozenset({"read", "edit", "shell", "agent", "mcp"})
    )


def _caps_with_command_policy():
    import dataclasses

    from rayspec.providers.capabilities import CLAUDE_CAPABILITIES

    return dataclasses.replace(CLAUDE_CAPABILITIES, extra=frozenset({"command_policy"}))
