"""``provider_options`` is a raw pass-through, so policy has to inspect it.

Both adapters apply an agent's ``provider_options`` block over the options rayspec computed. A
few lines of YAML inside the very workflow a policy governs can therefore put the denied tools,
the access level or the model straight back. Policy refuses that at load time.
"""

from __future__ import annotations

from .conftest import Tree, validated

WF = """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      model: claude-sonnet-4-5
      access: read-only
      provider_options:
        claude:
{options}
    prompt: hello
"""


def wf(options: str) -> str:
    return WF.format(options="\n".join(f"          {line}" for line in options.splitlines()))


def test_putting_the_denied_tools_back_is_an_error(tree: Tree) -> None:
    tree.policy("access:\n  max: read-only\ntools:\n  deny: [shell, web]\n")
    _, report = validated(
        tree,
        wf(
            "tools: null\n"
            "allowed_tools: [Bash, WebSearch]\n"
            "disallowed_tools: []\n"
            "permission_mode: bypassPermissions\n"
        ),
    )
    joined = "\n".join(report.errors)
    assert "provider_options.claude.allowed_tools" in joined
    assert "provider_options.claude.disallowed_tools" in joined
    assert "provider_options.claude.permission_mode" in joined
    assert "tools.deny" in joined
    assert ".rayspec/policy.yaml:" in joined


def test_putting_the_denied_model_back_is_an_error(tree: Tree) -> None:
    tree.policy("models:\n  deny: ['*opus*']\n")
    _, report = validated(tree, wf("model: claude-opus-4-1\n"))
    joined = "\n".join(report.errors)
    assert "provider_options.claude.model" in joined
    assert "models.deny" in joined


def test_raising_the_access_level_back_is_an_error(tree: Tree) -> None:
    tree.policy("access:\n  max: read-only\n")
    _, report = validated(tree, wf("permission_mode: bypassPermissions\n"))
    joined = "\n".join(report.errors)
    assert "provider_options.claude.permission_mode" in joined
    assert "access.max" in joined


def test_a_provider_option_the_policy_does_not_control_is_fine(tree: Tree) -> None:
    tree.policy("access:\n  max: read-only\ntools:\n  deny: [web]\n")
    _, report = validated(tree, wf("max_thinking_tokens: 1024\n"))
    assert report.ok, report.errors


def test_provider_options_are_untouched_without_a_policy(tree: Tree) -> None:
    _, report = validated(tree, wf("allowed_tools: [Bash]\nmodel: claude-opus-4-1\n"))
    assert report.ok, report.errors


def test_a_block_for_another_provider_is_not_inspected(tree: Tree) -> None:
    """``provider_options.codex`` is dead weight on a claude agent; only its own block matters."""
    tree.policy("tools:\n  deny: [web]\n")
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      provider_options:
        codex:
          config: {tools: {web_search: true}}
    prompt: hello
""",
    )
    assert report.ok, report.errors


def test_the_codex_config_that_re_enables_web_is_an_error(tree: Tree) -> None:
    tree.policy("tools:\n  deny: [web]\n")
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: codex
      model: gpt-5.6
      provider_options:
        codex:
          config: {tools: {web_search: true}}
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    assert "provider_options.codex.config.tools" in joined


def test_a_policy_line_is_named_once_however_many_entries_it_holds(tree: Tree) -> None:
    tree.policy("tools:\n  deny: [shell, web, edit]\n")
    _, report = validated(tree, wf("allowed_tools: [Bash]\n"))
    (message,) = report.errors
    assert message.count(".rayspec/policy.yaml:2") == 1
