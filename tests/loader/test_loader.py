"""load_workflow: includes, agent resolution, tiers/aliases, prompt files, hash."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.config import Config
from rayspec.errors import LoaderError
from rayspec.loader import load_workflow
from rayspec.loader.bundled import bundled_dir
from rayspec.schema import IncludeStep, PromptStep, SchemaError

from .conftest import Tree

REPO_ROOT = Path(__file__).resolve().parents[2]

SIMPLE = """
rayspec: 1
name: simple
steps:
  - id: one
    prompt: hi
"""


def test_load_by_name_path_and_ref(tree: Tree):
    path = tree.workflow("simple", SIMPLE)
    by_name = load_workflow("simple", project_root=tree.root, home=tree.home)
    by_path = load_workflow(path, project_root=tree.root, home=tree.home)
    by_str_path = load_workflow(str(path), project_root=tree.root, home=tree.home)
    assert by_name.path == by_path.path == by_str_path.path == path
    assert by_name.workflow.name == "simple"
    assert by_name.hash == by_path.hash


def test_unknown_name_has_did_you_mean(tree: Tree):
    tree.workflow("review_pr", SIMPLE.replace("simple", "review_pr"))
    with pytest.raises(LoaderError, match="review_pr"):
        load_workflow("reviewpr", project_root=tree.root, home=tree.home)


def test_schema_errors_propagate_with_source(tree: Tree):
    tree.workflow("bad", "rayspec: 1\nname: bad\nsteps:\n  - id: a\n    shel: x\n")
    with pytest.raises(SchemaError, match=r"bad\.yaml"):
        load_workflow("bad", project_root=tree.root, home=tree.home)


# --- agents ---------------------------------------------------------------------------------


def test_agent_resolution_chain_and_defaults(tree: Tree):
    tree.agent("reviewer", "provider: claude\nmodel: small\naccess: read-only\n")
    tree.agent("writer", "provider: codex\nmodel: large\n", user=True)
    tree.agent("writer", "provider: codex\nmodel: small\n")  # project wins over user
    tree.agent("user_only", "provider: codex\neffort: low\n", user=True)
    tree.workflow(
        "wf",
        """
rayspec: 1
name: wf
defaults:
  agent: impl
agents:
  impl: {provider: codex, model: medium, max_turns: 5}
  reviewer: {provider: codex, model: large}   # shadows .rayspec/agents/reviewer.yaml
steps:
  - id: a
    prompt: default agent
  - id: b
    prompt: named workflow agent
    agent: reviewer
  - id: c
    prompt: project agent file
    agent: writer
  - id: d
    prompt: user agent file
    agent: user_only
  - id: e
    prompt: bare provider
    agent: claude
  - id: f
    prompt: override
    agent: {extends: impl, model: large, access: read-only}
  - id: g
    prompt: inline
    agent: {provider: claude, model: opus-x, effort: high}
""",
    )
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    a = rw.agent_for("a")
    assert (a.name, a.provider, a.model, a.max_turns) == ("impl", "codex", "gpt-5.4", 5)
    b = rw.agent_for("b")
    assert (b.provider, b.model) == ("codex", "gpt-5.4")
    assert b.effort == "high"  # large codex tier carries effort: high
    c = rw.agent_for("c")
    assert (c.provider, c.model, c.effort) == ("codex", "gpt-5.4", "low")
    assert ".rayspec/agents/writer.yaml" in c.source
    d = rw.agent_for("d")
    assert (d.provider, d.effort, d.model) == ("codex", "low", "gpt-5.4")
    e = rw.agent_for("e")
    assert (e.name, e.provider, e.model) == ("claude", "claude", "sonnet")
    f = rw.agent_for("f")
    assert (f.provider, f.model, f.access, f.max_turns) == ("codex", "gpt-5.4", "read-only", 5)
    assert f.effort == "high"
    g = rw.agent_for("g")
    assert (g.provider, g.model, g.effort, g.access) == (
        "claude",
        "opus-x",
        "high",
        "workspace-write",
    )
    # every prompt step has an agent
    assert set(rw.step_agents) == {"a", "b", "c", "d", "e", "f", "g"}


def test_shallow_merge_tools_and_provider_options_replace_wholesale(tree: Tree):
    tree.workflow(
        "wf",
        """
rayspec: 1
name: wf
agents:
  base:
    provider: claude
    tools: {deny: [web, shell]}
    provider_options: {claude: {setting_sources: [project]}}
    max_turns: 3
    instructions: base text
steps:
  - id: a
    prompt: x
    agent: {extends: base, tools: {allow: [read]}, provider_options: {codex: {a: 1}}}
  - id: b
    prompt: x
    agent: {extends: base, instructions_file: prompts/inst.md}
""",
    )
    tree.write("prompts/inst.md", "from file")
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    a = rw.agent_for("a")
    assert a.tools.allow == ["read"]
    assert a.tools.deny == []
    assert a.provider_options == {"codex": {"a": 1}}
    assert a.max_turns == 3
    assert a.instructions == "base text"
    b = rw.agent_for("b")
    assert b.instructions == "from file"


def test_unknown_agent_name_and_extends(tree: Tree):
    tree.workflow(
        "wf",
        "rayspec: 1\nname: wf\nagents:\n  reviewer: {provider: claude}\nsteps:\n  - id: a\n    prompt: x\n    agent: reviewr\n",
    )
    with pytest.raises(LoaderError) as ei:
        load_workflow("wf", project_root=tree.root, home=tree.home)
    assert "reviewr" in str(ei.value)
    assert "reviewer" in str(ei.value)


def test_tiers_and_aliases_from_config(tree: Tree):
    cfg = Config.parse(
        {
            "default_provider": "codex",
            "tiers": {"codex": {"small": {"model": "gpt-mini", "effort": "low"}}},
            "aliases": {"@fast": {"provider": "claude", "model": "haiku-x", "effort": "minimal"}},
        }
    )
    tree.workflow(
        "wf",
        """
rayspec: 1
name: wf
agents:
  tiny: {model: small}
  tiny_hi: {model: small, effort: high}
  aliased: {model: "@fast"}
  aliased_eff: {provider: claude, model: "@fast", effort: high}
steps:
  - id: a
    prompt: x
    agent: tiny
  - id: b
    prompt: x
    agent: tiny_hi
  - id: c
    prompt: x
    agent: aliased
  - id: d
    prompt: x
    agent: aliased_eff
  - id: e
    prompt: x
""",
    )
    rw = load_workflow("wf", project_root=tree.root, home=tree.home, config=cfg)
    assert (rw.agent_for("a").provider, rw.agent_for("a").model, rw.agent_for("a").effort) == (
        "codex",
        "gpt-mini",
        "low",
    )
    assert rw.agent_for("b").effort == "high"
    c = rw.agent_for("c")
    assert (c.provider, c.model, c.effort) == ("claude", "haiku-x", "minimal")
    d = rw.agent_for("d")
    assert (d.provider, d.model, d.effort) == ("claude", "haiku-x", "high")
    e = rw.agent_for("e")  # no agent anywhere → config.default_provider as a bare provider
    assert (e.name, e.provider, e.model) == ("codex", "codex", "gpt-5.4")


def test_alias_pinning_another_provider_than_the_agent_is_an_error(tree: Tree):
    cfg = Config.parse({"aliases": {"@mini": {"provider": "codex", "model": "gpt-5.4"}}})
    tree.workflow(
        "wf",
        "rayspec: 1\nname: wf\nsteps:\n  - id: s\n    prompt: x\n"
        "    agent: {provider: claude, model: '@mini'}\n",
    )
    with pytest.raises(LoaderError) as exc:
        load_workflow("wf", project_root=tree.root, home=tree.home, config=cfg)
    message = str(exc.value)
    assert "steps.s.agent" in message
    assert "'@mini'" in message and "'codex'" in message and "'claude'" in message
    assert exc.value.location == ".rayspec/workflows/wf.yaml:6"
    # an unset provider (config default) or the same provider is fine
    tree.workflow(
        "ok",
        "rayspec: 1\nname: ok\nsteps:\n  - id: s\n    prompt: x\n    agent: {model: '@mini'}\n"
        "  - id: t\n    prompt: x\n    agent: {provider: codex, model: '@mini'}\n",
    )
    rw = load_workflow("ok", project_root=tree.root, home=tree.home, config=cfg)
    assert rw.agent_for("s").provider == rw.agent_for("t").provider == "codex"
    assert rw.warnings == []


def test_unknown_alias_and_unconfigured_tier(tree: Tree):
    tree.workflow(
        "wf",
        "rayspec: 1\nname: wf\nagents:\n  a: {model: '@nope'}\nsteps:\n  - id: s\n    prompt: x\n    agent: a\n",
    )
    with pytest.raises(LoaderError, match="@nope"):
        load_workflow("wf", project_root=tree.root, home=tree.home)
    tree.workflow(
        "wf2",
        "rayspec: 1\nname: wf2\nagents:\n  a: {provider: other, model: large}\nsteps:\n  - id: s\n    prompt: x\n    agent: a\n",
    )
    cfg = Config.parse({"providers": {"other": {}}})
    rw = load_workflow("wf2", project_root=tree.root, home=tree.home, config=cfg)
    assert rw.agent_for("s").model is None
    assert any("tier" in w and "other" in w for w in rw.warnings)


def test_agent_locations_and_yaml_path(tree: Tree):
    tree.workflow(
        "wf",
        """rayspec: 1
name: wf
agents:
  implementer:
    provider: codex
    max_turns: 60
steps:
  - id: a
    prompt: x
    agent: implementer
  - id: b
    prompt: x
    agent:
      provider: codex
      budget_usd: 2
""",
    )
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    a = rw.agent_for("a")
    assert a.yaml_path == "agents.implementer"
    assert a.locations["max_turns"] == ".rayspec/workflows/wf.yaml:6"
    b = rw.agent_for("b")
    assert b.yaml_path == "steps.b.agent"
    assert b.locations["budget_usd"] == ".rayspec/workflows/wf.yaml:15"


# --- prompt files -------------------------------------------------------------------------


def test_prompt_file_is_read_relative_to_rayspec_dir(tree: Tree):
    tree.write("prompts/review.md", "Review {{ inputs.target }}")
    tree.workflow(
        "wf", "rayspec: 1\nname: wf\nsteps:\n  - id: a\n    prompt_file: prompts/review.md\n"
    )
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    assert rw.prompt_text("a") == "Review {{ inputs.target }}"
    assert rw.prompt_files["a"].path == tree.rayspec / "prompts/review.md"
    assert tree.rayspec / "prompts/review.md" in rw.source_files


def test_missing_prompt_file_is_loader_error(tree: Tree):
    tree.workflow(
        "wf", "rayspec: 1\nname: wf\nsteps:\n  - id: a\n    prompt_file: prompts/nope.md\n"
    )
    with pytest.raises(LoaderError, match=r"prompts/nope\.md"):
        load_workflow("wf", project_root=tree.root, home=tree.home)


# --- includes -------------------------------------------------------------------------------


BLOCK = """
rayspec: 1
name: review_block
inputs:
  target: {type: string, required: true}
  depth: {type: integer, default: 1}
agents:
  rev: {provider: claude, model: small}
steps:
  - id: lint
    shell: echo lint {{ inputs.target }}
  - id: judge
    needs: [lint]
    agent: rev
    prompt: judge {{ steps.lint.output }}
outputs:
  verdict: "{{ steps.judge.output }}"
"""


def test_include_expansion_keeps_body_and_outputs(tree: Tree):
    tree.workflow("review_block", BLOCK)
    tree.workflow(
        "main",
        """
rayspec: 1
name: main
steps:
  - id: prep
    shell: echo
  - id: review
    needs: [prep]
    include: review_block
    with: {target: "src/", depth: 2}
  - id: after
    needs: [review]
    shell: echo {{ steps.review.output.verdict }}
""",
    )
    rw = load_workflow("main", project_root=tree.root, home=tree.home)
    assert isinstance(rw.workflow.steps[1], IncludeStep)
    body = rw.includes["review"]
    assert body.workflow_name == "review_block"
    assert body.path == tree.rayspec / "workflows/review_block.yaml"
    assert body.inputs_binding == {"target": "src/", "depth": 2}
    assert [s.id for s in body.steps] == ["lint", "judge"]
    assert body.outputs == {"verdict": "{{ steps.judge.output }}"}
    assert set(body.inputs) == {"target", "depth"}
    # inner steps are addressed by include-scoped paths and have their agents resolved
    assert "review/judge" in rw.step_agents
    assert rw.agent_for("review/judge").model == "haiku"
    # inner step ids are NOT top-level steps
    assert [s.id for s in rw.workflow.steps] == ["prep", "review", "after"]
    paths = [g.prefix for g in rw.graphs()]
    assert "review/" in paths
    assert tree.rayspec / "workflows/review_block.yaml" in rw.source_files


def test_include_by_relative_path_and_nested_in_loop(tree: Tree):
    tree.workflow("review_block", BLOCK)
    tree.workflow(
        "main",
        """
rayspec: 1
name: main
steps:
  - id: build
    loop:
      max_iterations: 2
      steps:
        - id: review
          include: review_block.yaml
          with: {target: x}
""",
    )
    rw = load_workflow("main", project_root=tree.root, home=tree.home)
    assert "build/review" in rw.includes
    assert "build/review/judge" in rw.step_agents


def test_include_cycle_is_error(tree: Tree):
    tree.workflow("a", "rayspec: 1\nname: a\nsteps:\n  - id: x\n    include: b\n")
    tree.workflow("b", "rayspec: 1\nname: b\nsteps:\n  - id: y\n    include: a\n")
    with pytest.raises(LoaderError, match="cycle"):
        load_workflow("a", project_root=tree.root, home=tree.home)


def test_include_self_cycle_is_error(tree: Tree):
    tree.workflow("a", "rayspec: 1\nname: a\nsteps:\n  - id: x\n    include: a\n")
    with pytest.raises(LoaderError, match="cycle"):
        load_workflow("a", project_root=tree.root, home=tree.home)


def test_include_depth_limit(tree: Tree):
    for i in range(11):
        tree.workflow(
            f"w{i}", f"rayspec: 1\nname: w{i}\nsteps:\n  - id: x\n    include: w{i + 1}\n"
        )
    tree.workflow("w11", "rayspec: 1\nname: w11\nsteps:\n  - id: x\n    shell: echo\n")
    with pytest.raises(LoaderError, match="depth"):
        load_workflow("w0", project_root=tree.root, home=tree.home)
    # 8 levels are fine
    rw = load_workflow("w3", project_root=tree.root, home=tree.home)
    assert rw.includes


def test_include_unknown_target(tree: Tree):
    tree.workflow("a", "rayspec: 1\nname: a\nsteps:\n  - id: x\n    include: nope\n")
    with pytest.raises(LoaderError, match="nope"):
        load_workflow("a", project_root=tree.root, home=tree.home)


# --- hash ----------------------------------------------------------------------------------


def test_hash_is_stable_and_covers_all_files(tree: Tree):
    tree.workflow("review_block", BLOCK)
    tree.agent("impl", "provider: codex\ninstructions_file: prompts/impl.md\n")
    tree.write("prompts/impl.md", "be nice")
    tree.write("prompts/p.md", "prompt")
    tree.workflow(
        "main",
        """
rayspec: 1
name: main
steps:
  - id: a
    prompt_file: prompts/p.md
    agent: impl
  - id: r
    include: review_block
    with: {target: x}
""",
    )
    h1 = load_workflow("main", project_root=tree.root, home=tree.home).hash
    h2 = load_workflow("main", project_root=tree.root, home=tree.home).hash
    assert h1 == h2
    assert len(h1) == 64
    rw = load_workflow("main", project_root=tree.root, home=tree.home)
    assert {p.name for p in rw.source_files} == {
        "main.yaml",
        "review_block.yaml",
        "impl.yaml",
        "impl.md",
        "p.md",
    }
    tree.write("prompts/impl.md", "be nicer")
    assert load_workflow("main", project_root=tree.root, home=tree.home).hash != h1
    tree.write("prompts/impl.md", "be nice")
    tree.write("prompts/p.md", "prompt!")
    assert load_workflow("main", project_root=tree.root, home=tree.home).hash != h1
    tree.write("prompts/p.md", "prompt")
    assert load_workflow("main", project_root=tree.root, home=tree.home).hash == h1


def test_prompt_steps_in_bodies_get_agents(tree: Tree):
    tree.workflow(
        "wf",
        """
rayspec: 1
name: wf
steps:
  - id: build
    loop:
      max_iterations: 2
      steps:
        - id: implement
          prompt: x
  - id: fan
    each: inputs.items
    steps:
      - id: patch
        prompt: y
        agent: codex
""",
    )
    rw = load_workflow("wf", project_root=tree.root, home=tree.home)
    assert rw.agent_for("build/implement").provider == "claude"
    assert rw.agent_for("fan/patch").provider == "codex"
    step = rw.step("fan/patch")
    assert isinstance(step, PromptStep)
    assert rw.location_of("fan/patch", "agent") == ".rayspec/workflows/wf.yaml:16"


# --- agent keys -------------------------------------------------------------------------------


def test_agent_keys_distinguish_origins(tree: Tree):
    """A main-document ``agents.foo`` must not capture an included step's agent file ``foo``."""
    tree.agent("foo", "provider: codex\nmodel: small\n")
    tree.agent("bar", "provider: codex\n", user=True)
    tree.workflow(
        "sub",
        """
rayspec: 1
name: sub
agents:
  bar: {provider: stub}
steps:
  - id: inner
    prompt: x
    agent: foo
  - id: inner_bar
    prompt: x
    agent: bar
""",
    )
    tree.workflow(
        "main",
        """
rayspec: 1
name: main
agents:
  foo: {provider: claude}
  bar: {provider: claude}
steps:
  - id: a
    prompt: x
    agent: foo
  - id: b
    prompt: x
    agent: bar
  - id: inc
    include: sub
  - id: c
    prompt: x
    agent: foo
""",
    )
    rw = load_workflow("main", project_root=tree.root, home=tree.home)
    assert rw.agent_for("a").provider == "claude"
    assert rw.agent_for("c").provider == "claude"
    assert rw.agent_for("a") is rw.agent_for("c")
    inner = rw.agent_for("inc/inner")
    assert inner.provider == "codex"
    assert ".rayspec/agents/foo.yaml" in inner.source
    assert rw.agent_for("b").provider == "claude"
    assert rw.agent_for("inc/inner_bar").provider == "stub"
    assert len({rw.step_agents[p] for p in ("a", "b", "inc/inner", "inc/inner_bar")}) == 4


def test_bundled_label_is_install_independent(tree: Tree):
    """A bundled workflow is labelled `<bundled>/<name>.yaml` wherever the package sits — the
    label is mixed into the hash and keyed in trusted.yaml, so it must not be a machine path.
    The checkout is the sharp case: there the bundled file IS project-relative."""
    rw = load_workflow("pr_review", project_root=tree.root, home=tree.home)
    assert rw.label == "<bundled>/pr_review.yaml"
    assert all(p.is_relative_to(bundled_dir()) for p in rw.source_files), rw.source_files
    assert rw.includes["review"].path == bundled_dir() / "review_block.yaml"
    from_checkout = load_workflow("pr_review", project_root=REPO_ROOT, home=tree.home)
    assert from_checkout.label == "<bundled>/pr_review.yaml"
    assert from_checkout.hash == rw.hash


def test_a_bundled_label_resolves_back_to_the_file(tree: Tree):
    """`trust list` re-loads a trusted workflow by the label it recorded."""
    rw = load_workflow("<bundled>/pr_review.yaml", project_root=tree.root, home=tree.home)
    assert rw.path == bundled_dir() / "pr_review.yaml"
    with pytest.raises(LoaderError, match=r"workflow file not found: <bundled>/nope\.yaml"):
        load_workflow("<bundled>/nope.yaml", project_root=tree.root, home=tree.home)
