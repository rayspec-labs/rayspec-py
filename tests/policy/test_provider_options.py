"""``provider_options`` is a raw pass-through, so policy has to inspect it.

Both adapters apply an agent's ``provider_options`` block over the options rayspec computed. A
few lines of YAML inside the very workflow a policy governs can therefore put the denied tools,
the access level or the model straight back. Policy refuses that at load time.
"""

from __future__ import annotations

import pytest

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


def test_moving_an_mcp_server_into_provider_options_is_an_error(tree: Tree) -> None:
    """``mcp.allow_servers`` has to see the servers the adapter merges in from the raw block.

    Declaring the server honestly under ``agent.mcp`` is refused; the identical server under
    ``provider_options.claude.mcp_servers`` reaches the SDK verbatim unless policy looks there
    too, because ``mcp_servers`` is merged rather than replaced.
    """
    tree.policy("mcp:\n  allow_servers: [github]\n")
    _, report = validated(
        tree,
        wf("mcp_servers:\n  evil: {type: stdio, command: /bin/sh}\n"),
    )
    joined = "\n".join(report.errors)
    assert "provider_options.claude.mcp_servers" in joined
    assert "mcp.allow_servers" in joined
    assert ".rayspec/policy.yaml:" in joined


def test_moving_an_mcp_server_into_the_codex_config_is_an_error(tree: Tree) -> None:
    tree.policy("mcp:\n  allow_servers: [github]\n")
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
          config:
            mcp_servers:
              evil: {command: /bin/sh}
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    assert "provider_options.codex.config.mcp_servers" in joined
    assert "mcp.allow_servers" in joined


def test_putting_the_web_tools_back_defeats_network_off_without_any_policy(tree: Tree) -> None:
    """``network: off`` is a workflow control, so it is protected with no policy file at all."""
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      access: read-only
      network: off
      provider_options:
        claude:
          disallowed_tools: []
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    assert "provider_options.claude.disallowed_tools" in joined
    assert "network" in joined


def test_re_enabling_codex_web_search_defeats_network_off_without_any_policy(tree: Tree) -> None:
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: codex
      model: gpt-5.6
      network: off
      provider_options:
        codex:
          config: {tools: {web_search: true}}
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    assert "provider_options.codex.config.tools" in joined
    assert "network" in joined


CODEX_WF = """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: codex
      model: gpt-5.6
{fields}      provider_options:
        codex:
{options}
    prompt: hello
"""


def codex_wf(options: str, *, fields: str = "") -> str:
    """A one-step codex workflow whose ``provider_options.codex`` block is ``options``."""
    return CODEX_WF.format(
        fields="".join(f"      {line}\n" for line in fields.splitlines()),
        options="\n".join(f"          {line}" for line in options.splitlines()),
    )


# -- the class, not the spelling: anything rayspec cannot reason about ---------------------------


def test_extra_args_re_emitting_a_controlled_flag_is_refused(tree: Tree) -> None:
    """``extra_args`` is appended to argv AFTER the flags rayspec computed, so last wins.

    The reproduction: ``network: off`` computes ``--disallowedTools WebFetch,WebSearch`` and
    ``extra_args`` then appends an empty ``--disallowedTools``, which the CLI takes instead.
    """
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      network: off
      provider_options:
        claude:
          extra_args: {disallowedTools: ""}
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    assert "provider_options.claude.extra_args" in joined
    assert "network: off" in joined


@pytest.mark.parametrize(
    "option",
    [
        "extra_args: {model: claude-opus-4-1}",
        'settings: \'{"permissions": {"defaultMode": "bypassPermissions"}}\'',
        "fallback_model: claude-opus-4-1",
        "add_dirs: [/]",
        "permission_prompt_tool_name: mcp__evil__approve",
        "agents: {sneaky: {description: d, prompt: p}}",
        "hooks: {PreToolUse: []}",
        "sandbox: {enabled: false}",
        "plugins: [{type: local, path: /tmp/p}]",
        "setting_sources: [user, project, local]",
        "continue_conversation: true",
    ],
)
def test_a_claude_option_rayspec_cannot_reason_about_is_refused_under_a_control(
    tree: Tree, option: str
) -> None:
    """The allowlist covers the next spelling too: enumerating dangerous keys never finishes."""
    tree.policy("access:\n  max: read-only\n")
    _, report = validated(tree, wf(f"{option}\n"))
    key = option.split(":", 1)[0]
    joined = "\n".join(report.errors)
    assert f"provider_options.claude.{key}" in joined, joined
    assert "access.max" in joined


def test_a_codex_config_key_rayspec_cannot_reason_about_is_refused_under_a_control(
    tree: Tree,
) -> None:
    tree.policy("models:\n  deny: ['*opus*']\n")
    _, report = validated(tree, codex_wf("config: {shell_environment_policy: {inherit: all}}\n"))
    joined = "\n".join(report.errors)
    assert "provider_options.codex.config.shell_environment_policy" in joined
    assert "models.deny" in joined


def test_the_refusal_says_which_keys_are_permitted(tree: Tree) -> None:
    """A control that blocks everything without saying what is left teaches people to switch it
    off. The message names the keys rayspec does reason about."""
    tree.policy("access:\n  max: read-only\n")
    _, report = validated(tree, wf("extra_args: {model: claude-opus-4-1}\n"))
    (message,) = report.errors
    assert "env" in message
    assert "mcp_servers" in message


# -- one normalisation, so a nesting variant cannot diverge --------------------------------------


def test_an_extra_nesting_level_does_not_hide_the_codex_block(tree: Tree) -> None:
    """``provider_options.codex.codex.…`` is the block the adapter reads, so it is the block the
    check reads. Walking one shape while the adapter accepts two is an unguarded pass-through."""
    tree.policy("mcp:\n  allow_servers: [github]\n")
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
          codex:
            config:
              mcp_servers:
                evil: {command: /bin/sh}
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    assert "mcp_servers" in joined
    assert "mcp.allow_servers" in joined


def test_the_adapter_and_the_check_narrow_a_block_the_same_way() -> None:
    from rayspec.schema import provider_option_block

    doubled = {"codex": {"config": {"mcp_servers": {"evil": {"command": "/bin/sh"}}}}}
    assert provider_option_block("codex", doubled) == doubled["codex"]
    assert provider_option_block("codex", {"config": {}}) == {"config": {}}


# -- a control that blocks the PERMITTED case is its own defect ----------------------------------


def test_an_allowed_mcp_server_may_be_added_through_provider_options(tree: Tree) -> None:
    """``mcp.allow_servers: [github]`` permits ``github`` — wherever it is declared."""
    tree.policy("mcp:\n  allow_servers: [github]\n")
    _, report = validated(
        tree,
        wf("mcp_servers:\n  github: {type: stdio, command: github-mcp-server}\n"),
    )
    assert report.ok, report.errors


def test_an_allowed_mcp_server_may_be_added_through_the_codex_config(tree: Tree) -> None:
    tree.policy("mcp:\n  allow_servers: [github]\n")
    _, report = validated(
        tree, codex_wf("config:\n  mcp_servers:\n    github: {command: github-mcp-server}\n")
    )
    assert report.ok, report.errors


def test_only_the_refused_server_is_named(tree: Tree) -> None:
    tree.policy("mcp:\n  allow_servers: [github]\n")
    _, report = validated(
        tree,
        wf(
            "mcp_servers:\n  github: {type: stdio, command: g}\n  evil: {type: stdio, command: e}\n"
        ),
    )
    (message,) = report.errors
    assert "evil" in message


def test_extra_environment_variables_stay_usable_under_every_control(tree: Tree) -> None:
    tree.policy(
        "access:\n  max: read-only\ntools:\n  deny: [web]\nmodels:\n  deny: ['*opus*']\n"
        "mcp:\n  allow_servers: [github]\n"
    )
    _, report = validated(tree, wf("env:\n  GITHUB_TOKEN: ${GH}\n"))
    assert report.ok, report.errors


def test_a_codex_agent_may_still_carry_its_accounting_options(tree: Tree) -> None:
    tree.policy("tools:\n  deny: [web]\n")
    _, report = validated(
        tree, codex_wf("ephemeral: true\nusage_baseline: {input: 10, output: 2}\n")
    )
    assert report.ok, report.errors


# -- an option that escalates the very control it sits under -------------------------------------


def test_codex_cannot_open_the_sandbox_network_gate_under_network_off(tree: Tree) -> None:
    """``network: off`` is not a firewall — but the workflow must not switch off the sandbox gate
    that would have stopped it, from inside the workflow the control governs."""
    _, report = validated(
        tree,
        codex_wf(
            "config:\n  sandbox_workspace_write: {network_access: true}\n", fields="network: off\n"
        ),
    )
    joined = "\n".join(report.errors)
    assert "provider_options.codex.config.sandbox_workspace_write" in joined
    assert "network: off" in joined


def test_codex_auto_review_is_refused_under_an_access_cap(tree: Tree) -> None:
    """``auto_review`` answers the sandbox escalation prompts for the agent."""
    tree.policy("access:\n  max: read-only\n")
    _, report = validated(
        tree, codex_wf("approval_mode: auto_review\n", fields="access: read-only\n")
    )
    joined = "\n".join(report.errors)
    assert "provider_options.codex.approval_mode" in joined
    assert "access.max" in joined


def test_codex_deny_all_stays_permitted_under_an_access_cap(tree: Tree) -> None:
    tree.policy("access:\n  max: read-only\n")
    _, report = validated(tree, codex_wf("approval_mode: deny_all\n", fields="access: read-only\n"))
    assert report.ok, report.errors


def test_the_escape_hatch_is_untouched_when_no_control_is_in_force(tree: Tree) -> None:
    """No policy file and no self-imposed control: `provider_options` passes through as before."""
    _, report = validated(tree, wf("extra_args: {disallowedTools: ''}\nadd_dirs: [/]\n"))
    assert report.ok, report.errors


def _claude_option_fields() -> list[str]:
    """Every ``ClaudeAgentOptions`` field name, read off the SDK dataclass."""
    from dataclasses import fields

    from claude_agent_sdk import ClaudeAgentOptions

    return sorted(f.name for f in fields(ClaudeAgentOptions))


@pytest.mark.parametrize("name", _claude_option_fields())
def test_a_claude_sdk_field_outside_the_allow_list_is_refused_under_a_control(
    tree: Tree, name: str
) -> None:
    """The class, checked against the real SDK surface rather than a list of spellings.

    Both sides are read rather than written down: the field names off the SDK dataclass, the
    allow-list off ``ALLOWED_PROVIDER_OPTIONS``. A field the SDK adds tomorrow is outside the
    list until someone can say what it does, which is what closes the class instead of closing
    one more spelling of it.
    """
    from rayspec.policy import ALLOWED_PROVIDER_OPTIONS

    allowed = {path[0] for path in ALLOWED_PROVIDER_OPTIONS["claude"] if len(path) == 1}
    assert {"extra_args", "settings"}.isdisjoint(allowed)  # the two that started this
    tree.policy("access:\n  max: read-only\n")
    _, report = validated(tree, wf(f"{name}: null\n"))
    refused = any(f"provider_options.claude.{name} is refused" in e for e in report.errors)
    assert refused is (name not in allowed), report.errors


def test_an_allow_listed_claude_option_really_reaches_the_sdk(tmp_path) -> None:
    """An allow-list that promises a key the adapter drops would be a lie in the other direction."""
    from claude_agent_sdk import ClaudeAgentOptions

    from rayspec.policy import ALLOWED_PROVIDER_OPTIONS
    from rayspec.providers.base import AgentRequest
    from rayspec.providers.claude import ClaudeProvider, build_options

    values: dict[str, object] = {
        "env": {"GH_TOKEN": "x"},
        "mcp_servers": {"docs": {"type": "stdio", "command": "d"}},
        "max_thinking_tokens": 4096,
        "max_buffer_size": 1024,
        "load_timeout_ms": 5000,
        "user": "someone",
    }
    names = sorted(path[0] for path in ALLOWED_PROVIDER_OPTIONS["claude"])
    assert sorted(values) == names, "keep this test in step with the allow-list"
    options, translation = build_options(
        ClaudeProvider({}),
        AgentRequest(
            step_path="s",
            prompt="hi",
            cwd=str(tmp_path),
            provider_options={"claude": values},
        ),
        stderr=lambda _line: None,
    )
    assert isinstance(options, ClaudeAgentOptions)
    for name in names:
        got = getattr(options, name)
        if name in {"env", "mcp_servers"}:  # merged under rayspec's own entries
            assert set(values[name]) <= set(got)  # type: ignore[arg-type]
        else:
            assert got == values[name]
    assert not [w for w in translation.warnings if "provider_options" in w], translation.warnings
