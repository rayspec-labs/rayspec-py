"""The policy pass of ``validate_workflow``: a violation is a load-time error, not a surprise."""

from __future__ import annotations

from rayspec.loader import load_workflow, validate_workflow
from rayspec.policy import EffectivePolicy, load_policy

from .conftest import Tree, validated

WF = """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: {provider}
      model: {model}
      access: {access}
    prompt: hello
"""


def wf(*, provider: str = "claude", model: str = "claude-sonnet-4-5", access: str = "read-only"):
    return WF.format(provider=provider, model=model, access=access)


def test_no_policy_leaves_validation_untouched(tree: Tree) -> None:
    _, report = validated(tree, wf())
    assert report.ok
    assert report.warnings == []


def test_provider_not_allowed_is_an_error_naming_the_layer(tree: Tree) -> None:
    tree.policy("providers:\n  allow: [claude]\n")
    _, report = validated(tree, wf(provider="codex", model="gpt-5.6"))
    joined = "\n".join(report.errors)
    assert "provider 'codex' is not allowed by policy" in joined
    assert ".rayspec/policy.yaml:2" in joined
    assert "steps.think.agent.provider" in joined
    assert "at .rayspec/workflows/wf.yaml:" in joined


def test_denied_model_is_an_error_with_the_matching_glob(tree: Tree) -> None:
    tree.policy("models:\n  deny:\n    - '*opus*'\n", user=True)
    _, report = validated(tree, wf(model="claude-opus-4-1"))
    joined = "\n".join(report.errors)
    assert "model 'claude-opus-4-1' is denied by policy" in joined
    assert "'*opus*'" in joined
    assert "~/.rayspec/policy.yaml:3" in joined


def test_access_above_the_cap_is_an_error(tree: Tree) -> None:
    tree.policy("access:\n  max: read-only\n")
    _, report = validated(tree, wf(access="full"))
    joined = "\n".join(report.errors)
    assert "access 'full' exceeds the policy maximum 'read-only'" in joined
    assert ".rayspec/policy.yaml:2" in joined


def test_all_three_layers_are_named_when_all_of_them_forbid(tree: Tree, tmp_path) -> None:
    env_file = tmp_path / "strict.yaml"
    env_file.write_text("providers:\n  allow: [stub]\n", encoding="utf-8")
    tree.policy("providers:\n  allow: [stub]\n", user=True)
    tree.policy("providers:\n  allow: [stub]\n")
    tree.workflow("wf", wf(provider="codex", model="gpt-5.6"))
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    effective = load_policy(tree.root, home=tree.home, environ={"RAYSPEC_POLICY": str(env_file)})
    report = validate_workflow(rw, policy=effective)
    joined = "\n".join(report.errors)
    assert str(env_file) in joined
    assert ".rayspec/policy.yaml:2" in joined
    assert "~/.rayspec/policy.yaml:2" in joined


def test_allowing_a_denied_tool_is_an_error(tree: Tree) -> None:
    tree.policy("tools:\n  deny: [web]\n")
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      tools:
        allow: [read, web]
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    assert "tool 'web' is denied by policy" in joined
    assert ".rayspec/policy.yaml:2" in joined


def test_a_denied_tool_is_added_to_the_agents_deny_list(tree: Tree) -> None:
    """The policy does not only complain: the agent really goes to the provider without it."""
    tree.policy("tools:\n  deny: [web]\n")
    rw, report = validated(tree, wf())
    assert report.ok
    agent = rw.agent_for("think")
    assert "web" in agent.tools.deny


def test_a_tool_the_provider_cannot_deny_is_reported_as_advisory(tree: Tree) -> None:
    tree.policy("tools:\n  deny: [edit]\n")
    rw, report = validated(tree, wf(provider="codex", model="gpt-5.6"))
    joined = "\n".join(report.warnings)
    assert "tools.deny: 'edit'" in joined
    assert "advisory" in joined
    assert "codex" in joined
    assert "edit" not in rw.agent_for("think").tools.deny


def test_an_mcp_server_outside_the_allow_list_is_an_error(tree: Tree) -> None:
    tree.policy("mcp:\n  allow_servers: [github]\n")
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      mcp:
        jira:
          command: jira-mcp
    prompt: hello
""",
    )
    joined = "\n".join(report.errors)
    assert "MCP server 'jira' is not allowed by policy" in joined
    assert ".rayspec/policy.yaml:2" in joined


def test_an_mcp_tool_entry_outside_the_allow_list_is_an_error(tree: Tree) -> None:
    tree.policy("mcp:\n  allow_servers: [github]\n")
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      tools:
        allow: ['mcp:jira/create']
    prompt: hello
""",
    )
    assert "MCP server 'jira' is not allowed by policy" in "\n".join(report.errors)


def test_an_empty_allow_list_names_every_layer_that_makes_it_unsatisfiable(tree: Tree) -> None:
    tree.policy("providers:\n  allow: []\n")
    _, report = validated(tree, wf())
    joined = "\n".join(report.errors)
    assert "provider 'claude' is not allowed by policy" in joined
    assert "(nothing)" in joined


def test_an_explicit_empty_policy_disables_discovery(tree: Tree) -> None:
    tree.policy("providers:\n  allow: [nobody]\n")
    tree.workflow("wf", wf())
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    report = validate_workflow(rw, policy=EffectivePolicy())
    assert report.ok


def _request_for(rw, path: str = "think"):
    """The neutral provider request the engine would build for ``path``."""
    from rayspec.engine.executors.prompt import build_request
    from rayspec.providers.stub import StubProvider

    return build_request(
        rw.step(path),
        rw.agent_for(path),
        StubProvider(),
        path=path,
        prompt="hello",
        instructions=None,
        env={},
        cwd=".",
        resume_session=None,
        timeout_s=None,
        run_id="r",
        attempt=1,
    )


def test_the_policy_denial_reaches_the_provider_request(tree: Tree) -> None:
    """End of the enforcement path: the provider is really told to deny the tool."""
    tree.policy("tools:\n  deny: [web]\n")
    rw, report = validated(tree, wf())
    assert report.ok
    assert "web" in _request_for(rw).tools.deny


def test_network_off_reaches_the_provider_request(tree: Tree) -> None:
    rw, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - id: think
    agent:
      provider: claude
      network: off
    prompt: hello
""",
    )
    assert report.ok
    assert "web" in _request_for(rw).tools.deny


def test_a_workspace_key_warns_that_nothing_enforces_it(tree: Tree) -> None:
    """The change guard is a library in this build; a policy key that does nothing must say so."""
    tree.policy("workspace:\n  protected_paths: ['.github/**']\n  max_changed_files: 2\n")
    _, report = validated(tree, wf())
    assert report.ok, report.errors
    joined = "\n".join(report.warnings)
    assert "the change guard is not run by this build" in joined
    assert ".rayspec/policy.yaml:" in joined


def test_the_workspace_warning_is_emitted_once_for_the_whole_run(tree: Tree) -> None:
    tree.policy("workspace:\n  protected_paths: ['a/**', 'b/**']\n")
    _, report = validated(
        tree,
        """rayspec: 1
name: wf
steps:
  - {id: one, agent: {provider: claude}, prompt: hello}
  - {id: two, agent: {provider: claude}, prompt: hello}
""",
    )
    assert sum("change guard is not run" in w for w in report.warnings) == 1
