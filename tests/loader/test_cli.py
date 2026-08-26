"""CLI commands (workflows, agents, validate, plan) on a temp project tree."""

from __future__ import annotations

import json
import sys
import types

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import _loader_common

from .conftest import Tree
from .fakes import capabilities_for

runner = CliRunner()


def _run(tree: Tree, *args: str):
    return runner.invoke(
        app, [*args, "--root", str(tree.root)], env={"RAYSPEC_HOME": str(tree.home)}
    )


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _loader_common,
        "capability_source",
        lambda: _loader_common.CapabilitySource(capabilities_for, ["claude", "codex", "stub"]),
    )


def test_workflows_lists_and_json(tree: Tree):
    tree.workflow(
        "review",
        "rayspec: 1\nname: review\ndescription: Review stuff\nsteps:\n  - id: a\n    shell: echo\n",
    )
    tree.workflow(
        "user_wf",
        "rayspec: 1\nname: user_wf\ndescription: from home\nsteps:\n  - id: a\n    shell: echo\n",
        user=True,
    )
    tree.workflow("broken", "a: [\n")
    res = _run(tree, "workflows")
    assert res.exit_code == 0, res.output
    assert "review" in res.output and "Review stuff" in res.output and "project" in res.output
    assert "user_wf" in res.output and "user" in res.output
    assert "broken" in res.output and "error" in res.output
    res = _run(tree, "workflows", "--json")
    data = json.loads(res.output)
    assert [d["name"] for d in data if d["scope"] != "bundled"] == ["broken", "review", "user_wf"]
    assert {d["name"]: d for d in data}["review"]["scope"] == "project"
    assert [d["name"] for d in data if d["scope"] == "bundled"] == [
        "fix_issue",
        "pr_review",
        "release_check",
        "resolve_conflicts",
        "review_block",
        "review_panel",
    ]


def test_workflows_empty(tree: Tree):
    res = _run(tree, "workflows")
    assert res.exit_code == 0
    assert "no project workflows yet" in res.output
    assert "pr_review" in res.output and "bundled" in res.output


def test_agents_lists(tree: Tree):
    tree.agent("reviewer", "provider: claude\nmodel: small\naccess: read-only\n")
    tree.agent("writer", "provider: codex\n", user=True)
    tree.agent("bad", "provider: [\n")
    res = _run(tree, "agents")
    assert res.exit_code == 0, res.output
    assert "reviewer" in res.output and "claude" in res.output and "small" in res.output
    assert "writer" in res.output and "codex" in res.output
    assert "bad" in res.output and "error" in res.output
    data = json.loads(_run(tree, "agents", "--json").output)
    assert {d["name"] for d in data} == {"reviewer", "writer", "bad"}


def test_validate_all_and_exit_codes(tree: Tree, fake_registry):
    tree.workflow("good", "rayspec: 1\nname: good\nsteps:\n  - id: a\n    shell: echo\n")
    tree.workflow(
        "bad",
        "rayspec: 1\nname: bad\nagents:\n  i: {provider: codex, max_turns: 3}\nsteps:\n  - id: a\n    needs: [zzz]\n    prompt: x\n    agent: i\n",
    )
    res = _run(tree, "validate")
    assert res.exit_code == 2, res.output
    assert "good" in res.output and "OK" in res.output
    assert "bad" in res.output and "FAILED" in res.output
    assert "unknown step 'zzz'" in res.output
    assert "unsupported: agents.i.max_turns = 3" in res.output
    assert "2 workflow(s) validated, 1 with errors" in res.output
    res = _run(tree, "validate", "good")
    assert res.exit_code == 0, res.output
    assert "no errors" in res.output
    res = _run(tree, "validate", "bad", "--allow-unsupported")
    assert res.exit_code == 2  # the needs error remains
    assert "warnings:" in res.output


def test_validate_allow_unsupported_turns_green(tree: Tree, fake_registry):
    tree.workflow(
        "w",
        "rayspec: 1\nname: w\nagents:\n  i: {provider: codex, max_turns: 3}\nsteps:\n  - id: a\n    prompt: x\n    agent: i\n",
    )
    res = _run(tree, "validate", "w", "--allow-unsupported")
    assert res.exit_code == 0, res.output
    assert "unsupported: agents.i.max_turns = 3" in res.output


def test_validate_load_failure(tree: Tree):
    tree.workflow("w", "rayspec: 1\nname: w\nsteps:\n  - id: a\n    shel: x\n")
    res = _run(tree, "validate", "w")
    assert res.exit_code == 2
    assert "FAILED to load" in res.output
    assert "shel" in res.output
    res = _run(tree, "validate", "missing")
    assert res.exit_code == 2
    assert "unknown workflow 'missing'" in res.output


def test_validate_without_registry_warns(tree: Tree, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        _loader_common,
        "capability_source",
        lambda: _loader_common.CapabilitySource(None, [], _loader_common.CAPABILITY_SKIP_WARNING),
    )
    tree.workflow("w", "rayspec: 1\nname: w\nsteps:\n  - id: a\n    prompt: x\n")
    res = _run(tree, "validate", "w")
    assert res.exit_code == 0, res.output
    assert "capability checks skipped (providers registry not available)" in res.output


def test_plan_renders(tree: Tree, fake_registry):
    tree.agent("impl", "provider: codex\nmodel: large\n")
    tree.workflow(
        "wf",
        """
rayspec: 1
name: wf
description: Plan me
inputs:
  issue: {type: integer, required: true}
  base: {type: string, default: main}
  tags: {type: array, default: []}
defaults:
  agent: impl
steps:
  - id: fetch
    shell: echo
  - id: assess
    needs: [fetch]
    agent: {provider: claude, model: small}
    prompt: x
    output_schema: {type: object}
  - id: build
    needs: [assess]
    when: steps.assess.output.ok
    loop:
      max_iterations: 3
      steps:
        - id: implement
          prompt: y
        - id: check
          needs: [implement]
          shell: pytest
  - id: confirm
    needs: [build]
    approve: ok?
""",
    )
    res = _run(tree, "plan", "wf", "--input", "issue=12", "--input", "tags=a", "-i", "tags=b")
    assert res.exit_code == 0, res.output
    out = res.output
    assert "workflow wf" in out and "Plan me" in out
    assert "issue = 12" in out and "base = main" in out and 'tags = ["a", "b"]' in out
    assert "impl" in out and "codex" in out and "gpt-5.4" in out and "high" in out
    assert "assess (inline)" in out and "haiku" in out
    assert "build/implement" in out and "build/check" in out
    assert "steps.assess.output.ok" in out
    assert "ok: every feature is supported" in out


def test_plan_missing_input_exits_2(tree: Tree, fake_registry):
    tree.workflow(
        "wf",
        "rayspec: 1\nname: wf\ninputs:\n  issue: {type: integer, required: true}\nsteps:\n  - id: a\n    shell: echo\n",
    )
    res = _run(tree, "plan", "wf")
    assert res.exit_code == 2
    assert "missing required input(s): issue" in res.output
    assert "missing (required)" in res.output
    res = _run(tree, "plan", "nope")
    assert res.exit_code == 2
    assert "unknown workflow 'nope'" in res.output


def test_plan_unsupported_and_allow(tree: Tree, fake_registry):
    tree.workflow(
        "wf",
        "rayspec: 1\nname: wf\nagents:\n  i: {provider: codex, max_turns: 3}\nsteps:\n  - id: a\n    prompt: x\n    agent: i\n",
    )
    res = _run(tree, "plan", "wf")
    assert res.exit_code == 2
    assert "1 unsupported feature error(s)" in res.output
    assert "unsupported: agents.i.max_turns = 3" in res.output
    res = _run(tree, "plan", "wf", "--allow-unsupported")
    assert res.exit_code == 0, res.output
    assert "1 unsupported feature warning(s)" in res.output
    # defaults.on_unsupported: warn downgrades too — the summary line must agree with the report
    tree.workflow(
        "wf2",
        "rayspec: 1\nname: wf2\ndefaults: {on_unsupported: warn}\nagents:\n"
        "  i: {provider: codex, max_turns: 3}\nsteps:\n  - id: a\n    prompt: x\n    agent: i\n",
    )
    res = _run(tree, "plan", "wf2")
    assert res.exit_code == 0, res.output
    assert "1 unsupported feature warning(s)" in res.output
    assert "error(s)" not in res.output


# --- lazy registry / templating imports ----------------------------------------------------


def _block_module(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setitem(sys.modules, name, None)  # import → ModuleNotFoundError


def test_capability_source_without_registry_warns(monkeypatch: pytest.MonkeyPatch):
    _block_module(monkeypatch, "rayspec.providers.registry")
    src = _loader_common.capability_source()
    assert src.capabilities_for is None
    assert src.warning == _loader_common.CAPABILITY_SKIP_WARNING


def test_capability_source_propagates_registry_bugs(monkeypatch: pytest.MonkeyPatch):
    broken = types.ModuleType("rayspec.providers.registry")

    def boom():
        raise RuntimeError("registry bug")

    broken.list_registrations = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rayspec.providers.registry", broken)
    with pytest.raises(RuntimeError, match="registry bug"):
        _loader_common.capability_source()


def test_template_checker_without_templating_is_none(monkeypatch: pytest.MonkeyPatch):
    _block_module(monkeypatch, "rayspec.templating")
    assert _loader_common.template_checker() is None


def test_template_checker_propagates_templating_bugs(monkeypatch: pytest.MonkeyPatch):
    broken = types.ModuleType("rayspec.templating")

    class Engine:
        def __init__(self) -> None:
            raise ImportError("jinja2 missing inside templating")

    broken.TemplateEngine = Engine  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rayspec.templating", broken)
    with pytest.raises(ImportError, match="jinja2 missing"):
        _loader_common.template_checker()


def test_default_known_providers_propagates_registry_bugs(monkeypatch: pytest.MonkeyPatch):
    from rayspec.config import Config
    from rayspec.loader.loader import default_known_providers

    _block_module(monkeypatch, "rayspec.providers.registry")
    assert {"claude", "codex", "stub"} <= default_known_providers(Config())
    broken = types.ModuleType("rayspec.providers.registry")

    def boom():
        raise RuntimeError("registry bug")

    broken.list_registrations = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rayspec.providers.registry", broken)
    with pytest.raises(RuntimeError, match="registry bug"):
        default_known_providers(Config())


def test_validate_without_names_skips_bundled_but_a_bundled_name_validates(
    tree: Tree, fake_registry
):
    """The shipped library is not the project's to validate on every `rayspec validate`, but it
    is there when named — and its `path` is the install-independent label."""
    tree.workflow("good", "rayspec: 1\nname: good\nsteps:\n  - id: a\n    shell: echo\n")
    res = _run(tree, "validate")
    assert res.exit_code == 0, res.output
    assert "1 workflow(s) validated, no errors" in res.output
    assert "pr_review" not in res.output
    res = _run(tree, "validate", "pr_review", "--json")
    assert res.exit_code == 0, res.output
    (row,) = json.loads(res.output)
    assert row["name"] == "pr_review" and row["ok"] is True
    assert row["path"] == "<bundled>/pr_review.yaml"


def test_short_path_and_workflow_label_name_bundled_files(tree: Tree):
    from rayspec.config import Config
    from rayspec.loader.bundled import bundled_dir

    checkout = bundled_dir().parents[2]  # <site>/rayspec/workflows/defaults -> <site>
    ctx = _loader_common.Context(project_root=checkout, home=tree.home, config=Config())
    assert (
        _loader_common.short_path(bundled_dir() / "pr_review.yaml", ctx)
        == "<bundled>/pr_review.yaml"
    )
    assert _loader_common.workflow_label("pr_review", ctx) == "<bundled>/pr_review.yaml"
