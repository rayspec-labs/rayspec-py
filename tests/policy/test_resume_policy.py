"""Resuming applies the policy that is in force now, not the one that was in force at launch.

A run that paused at a gate is the second half of the same run: ``resume``/``approve``/``reject``
re-load the workflow, so they have to put it through the same policy pass ``run`` did. Otherwise
pausing is a way to launder a workflow past every guardrail.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from rayspec.cli import _runs_common as common
from rayspec.cli.app import app
from rayspec.cli.commands.resume import guard_workflow_unchanged
from rayspec.store.file import FileRunStore

from .conftest import Tree
from .test_enforce import _request_for

runner = CliRunner()

GATE = """rayspec: 1
name: gate
isolation: none
steps:
  - {id: ok, approve: "ship?"}
  - id: think
    needs: [ok]
    agent: {provider: stub}
    prompt: hello
"""


@pytest.fixture
def paused(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> tuple[Tree, str]:
    """A run of ``gate`` stopped at its approval gate, with no policy file yet."""
    monkeypatch.setenv("RAYSPEC_HOME", str(tree.home))
    tree.workflow("gate", GATE)
    result = runner.invoke(app, ["run", "gate", "--root", str(tree.root), "--no-interactive"])
    assert result.exit_code == 3, result.output
    (slug_dir,) = [p for p in (tree.home / "projects").glob("*/*") if (p / "runs").is_dir()]
    (run_id,) = FileRunStore(slug_dir).list_run_ids()
    return tree, run_id


def test_approve_refuses_a_workflow_the_policy_now_forbids(paused: tuple[Tree, str]) -> None:
    tree, run_id = paused
    tree.policy("providers:\n  allow: [claude]\n")
    result = runner.invoke(app, ["approve", run_id, "ship it", "--root", str(tree.root)])
    assert result.exit_code == 2, result.output
    assert "not allowed by policy" in result.output


def test_resume_refuses_it_too(paused: tuple[Tree, str]) -> None:
    tree, run_id = paused
    tree.policy("providers:\n  allow: [claude]\n")
    result = runner.invoke(app, ["resume", run_id, "--yes", "--root", str(tree.root)])
    assert result.exit_code == 2, result.output
    assert "not allowed by policy" in result.output


def test_reject_refuses_it_too(paused: tuple[Tree, str]) -> None:
    """``reject`` resumes the engine as well (``on_reject: continue`` runs steps)."""
    tree, run_id = paused
    tree.policy("providers:\n  allow: [claude]\n")
    result = runner.invoke(app, ["reject", run_id, "no", "--root", str(tree.root)])
    assert result.exit_code == 2, result.output
    assert "not allowed by policy" in result.output


def test_an_untrusted_workflow_is_refused_on_resume(paused: tuple[Tree, str]) -> None:
    tree, run_id = paused
    tree.policy("trust:\n  require: true\n")
    result = runner.invoke(app, ["approve", run_id, "ship it", "--root", str(tree.root)])
    assert result.exit_code == 2, result.output
    assert "rayspec trust add" in result.output


def test_the_policy_tool_denial_is_folded_in_on_resume(paused: tuple[Tree, str]) -> None:
    """The half of the run after the gate reaches its provider with the policy's denials."""
    tree, run_id = paused
    tree.policy("tools:\n  deny: [web]\n")
    ctx = common.make_runs_context(tree.root)
    _, record = common.lookup_run(ctx, run_id)
    resolved = guard_workflow_unchanged(ctx, record, force=False)
    assert "web" in _request_for(resolved).tools.deny


def test_a_run_without_a_policy_still_resumes(paused: tuple[Tree, str]) -> None:
    tree, run_id = paused
    result = runner.invoke(app, ["approve", run_id, "ship it", "--root", str(tree.root)])
    assert result.exit_code == 0, result.output
