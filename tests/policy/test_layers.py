"""Policy discovery and layering: three layers, most-restrictive-wins, provenance per key."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.policy import PolicyError, load_policy, policy_paths

from .conftest import Tree


def test_no_policy_files_is_an_empty_policy(tree: Tree) -> None:
    eff = load_policy(tree.root, home=tree.home, environ={})
    assert eff.is_empty
    assert eff.layers == ()
    assert eff.provider_denied("claude") == ()
    assert eff.model_denied("gpt-5.6") == ()
    assert eff.max_access() is None


def test_policy_paths_names_the_three_layers(tree: Tree) -> None:
    env_path = tree.root / "strict.yaml"
    paths = policy_paths(tree.root, tree.home, {"RAYSPEC_POLICY": str(env_path)})
    assert [p.name for p in paths] == ["RAYSPEC_POLICY", "project", "user"]
    assert paths[0].path == env_path
    assert paths[1].path == tree.rayspec / "policy.yaml"
    assert paths[2].path == tree.home / "policy.yaml"


def test_providers_allow_intersects_across_layers(tree: Tree) -> None:
    tree.policy("providers:\n  allow: [claude, codex, stub]\n", user=True)
    tree.policy("providers:\n  allow: [claude, codex]\n")
    eff = load_policy(tree.root, home=tree.home, environ={})
    assert eff.allowed_providers() == frozenset({"claude", "codex"})
    assert eff.provider_denied("claude") == ()
    sources = eff.provider_denied("stub")
    assert [s.layer for s in sources] == ["project"]
    assert sources[0].location.endswith(".rayspec/policy.yaml:2")


def test_a_lower_layer_cannot_widen_a_higher_one(tree: Tree) -> None:
    """The user layer allowing more does not re-admit what the project layer excluded."""
    tree.policy("providers:\n  allow: [claude, codex, stub]\n", user=True)
    tree.policy("providers:\n  allow: [claude]\n")
    eff = load_policy(tree.root, home=tree.home, environ={})
    assert eff.allowed_providers() == frozenset({"claude"})
    assert [s.layer for s in eff.provider_denied("codex")] == ["project"]


def test_every_layer_that_forbids_is_named(tree: Tree) -> None:
    tree.policy("providers:\n  allow: [claude]\n", user=True)
    tree.policy("providers:\n  allow: [claude]\n")
    eff = load_policy(tree.root, home=tree.home, environ={})
    assert [s.layer for s in eff.provider_denied("codex")] == ["project", "user"]


def test_models_deny_is_the_union_and_names_the_matching_entry(tree: Tree) -> None:
    tree.policy("models:\n  deny:\n    - '*opus*'\n", user=True)
    tree.policy("models:\n  deny:\n    - gpt-5.6-pro\n")
    eff = load_policy(tree.root, home=tree.home, environ={})
    assert [s.value for s in eff.model_denied("claude-opus-4-1")] == ["*opus*"]
    assert [s.layer for s in eff.model_denied("claude-opus-4-1")] == ["user"]
    hit = eff.model_denied("gpt-5.6-pro")
    assert [s.layer for s in hit] == ["project"]
    assert hit[0].location.endswith(".rayspec/policy.yaml:3")
    assert eff.model_denied("gpt-5.6") == ()


def test_access_max_takes_the_lowest_level(tree: Tree) -> None:
    tree.policy("access:\n  max: full\n", user=True)
    tree.policy("access:\n  max: read-only\n")
    eff = load_policy(tree.root, home=tree.home, environ={})
    level, sources = eff.max_access() or ("", ())
    assert level == "read-only"
    assert [s.layer for s in sources] == ["project"]
    assert eff.access_exceeded("read-only") == ()
    assert [s.layer for s in eff.access_exceeded("workspace-write")] == ["project"]


def test_env_layer_wins_precedence_and_still_only_restricts(tree: Tree, tmp_path: Path) -> None:
    env_file = tmp_path / "strict.yaml"
    env_file.write_text("access:\n  max: read-only\n", encoding="utf-8")
    tree.policy("access:\n  max: full\n")
    eff = load_policy(tree.root, home=tree.home, environ={"RAYSPEC_POLICY": str(env_file)})
    level, sources = eff.max_access() or ("", ())
    assert level == "read-only"
    assert [s.layer for s in sources] == ["RAYSPEC_POLICY"]


def test_tools_deny_is_the_union(tree: Tree) -> None:
    tree.policy("tools:\n  deny: [web]\n", user=True)
    tree.policy("tools:\n  deny: [shell]\n")
    eff = load_policy(tree.root, home=tree.home, environ={})
    assert sorted(eff.denied_tools()) == ["shell", "web"]
    assert [s.layer for s in eff.tool_denied("web")] == ["user"]
    assert eff.tool_denied("read") == ()


def test_mcp_allow_servers_intersects(tree: Tree) -> None:
    tree.policy("mcp:\n  allow_servers: [github, jira]\n", user=True)
    tree.policy("mcp:\n  allow_servers: [github]\n")
    eff = load_policy(tree.root, home=tree.home, environ={})
    assert eff.allowed_mcp_servers() == frozenset({"github"})
    assert [s.layer for s in eff.mcp_denied("jira")] == ["project"]
    assert eff.mcp_denied("github") == ()


def test_change_guard_takes_the_smallest_limits_and_the_union_of_paths(tree: Tree) -> None:
    tree.policy(
        "workspace:\n"
        "  protected_paths: ['.github/**']\n"
        "  max_changed_files: 100\n"
        "  max_changed_lines: 5000\n",
        user=True,
    )
    tree.policy(
        "workspace:\n  protected_paths: ['infra/**']\n  max_changed_files: 20\n",
    )
    guard = load_policy(tree.root, home=tree.home, environ={}).change_guard()
    assert sorted(p for p, _ in guard.protected_paths) == [".github/**", "infra/**"]
    assert guard.max_changed_files is not None
    assert guard.max_changed_files[0] == 20
    assert [s.layer for s in guard.max_changed_files[1]] == ["project"]
    assert guard.max_changed_lines is not None
    assert guard.max_changed_lines[0] == 5000


def test_trust_required_when_any_layer_asks_for_it(tree: Tree) -> None:
    tree.policy("trust:\n  require: true\n", user=True)
    eff = load_policy(tree.root, home=tree.home, environ={})
    assert [s.layer for s in eff.trust_required()] == ["user"]
    assert not load_policy(tree.root, home=tree.home, environ={}).is_empty


def test_unknown_key_is_a_policy_error_naming_the_file_and_line(tree: Tree) -> None:
    tree.policy("providers:\n  allow: [claude]\nnetwrok:\n  deny: []\n")
    with pytest.raises(PolicyError) as excinfo:
        load_policy(tree.root, home=tree.home, environ={})
    assert "policy.yaml" in str(excinfo.value)
    assert "netwrok" in str(excinfo.value)


def test_missing_env_policy_file_is_an_error(tree: Tree, tmp_path: Path) -> None:
    with pytest.raises(PolicyError) as excinfo:
        load_policy(tree.root, home=tree.home, environ={"RAYSPEC_POLICY": str(tmp_path / "no")})
    assert "RAYSPEC_POLICY" in str(excinfo.value)


def test_the_same_file_named_twice_counts_once(tree: Tree) -> None:
    path = tree.policy("providers:\n  allow: [claude]\n")
    eff = load_policy(tree.root, home=tree.home, environ={"RAYSPEC_POLICY": str(path)})
    assert [layer.name for layer in eff.layers] == ["RAYSPEC_POLICY"]


def test_a_policy_path_that_is_a_directory_is_an_error(tree: Tree) -> None:
    """A layer that exists in some shape but is not a readable file must never be dropped."""
    (tree.rayspec / "policy.yaml").mkdir(parents=True)
    with pytest.raises(PolicyError) as exc:
        load_policy(tree.root, home=tree.home, environ={})
    assert "is a directory" in str(exc.value)
    assert ".rayspec/policy.yaml" in str(exc.value)


def test_a_dangling_symlink_policy_is_an_error(tree: Tree) -> None:
    tree.rayspec.mkdir(parents=True, exist_ok=True)
    (tree.rayspec / "policy.yaml").symlink_to(tree.rayspec / "gone.yaml")
    with pytest.raises(PolicyError) as exc:
        load_policy(tree.root, home=tree.home, environ={})
    assert "dangling symlink" in str(exc.value)


def test_a_symlink_loop_policy_is_an_error(tree: Tree) -> None:
    tree.rayspec.mkdir(parents=True, exist_ok=True)
    (tree.rayspec / "policy.yaml").symlink_to(tree.rayspec / "policy.yaml")
    with pytest.raises(PolicyError) as exc:
        load_policy(tree.root, home=tree.home, environ={})
    assert "symlink loop" in str(exc.value)


def test_the_user_layer_is_guarded_the_same_way(tree: Tree) -> None:
    (tree.home / "policy.yaml").mkdir(parents=True)
    with pytest.raises(PolicyError) as exc:
        load_policy(tree.root, home=tree.home, environ={})
    assert "is a directory" in str(exc.value)


def test_a_broken_layer_is_refused_through_the_cli(tree: Tree, monkeypatch) -> None:
    """The shape that matters: `rayspec validate` must not print OK when a layer vanished."""
    from typer.testing import CliRunner

    from rayspec.cli.app import app

    monkeypatch.setenv("RAYSPEC_HOME", str(tree.home))
    tree.workflow("wf", "rayspec: 1\nname: wf\nsteps:\n  - {id: go, shell: echo hi}\n")
    tree.rayspec.mkdir(parents=True, exist_ok=True)
    (tree.rayspec / "policy.yaml").symlink_to(tree.rayspec / "gone.yaml")
    result = CliRunner().invoke(app, ["validate", "wf", "--root", str(tree.root)])
    assert result.exit_code == 2, result.output
    assert "dangling symlink" in result.output


def test_a_genuinely_absent_layer_is_still_just_absent(tree: Tree) -> None:
    assert load_policy(tree.root, home=tree.home, environ={}).is_empty
