"""``rayspec trust add|list|remove|check`` — the CLI over ``.rayspec/trusted.yaml``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.policy import TrustStore, trusted_path

from .conftest import Tree

runner = CliRunner()

WF = """rayspec: 1
name: wf
steps:
  - id: go
    shell: echo hi
"""


@pytest.fixture
def project(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> Tree:
    monkeypatch.setenv("RAYSPEC_HOME", str(tree.home))
    tree.workflow("wf", WF)
    return tree


def run(*args: str):
    """Invoke the CLI with ``args`` and return the click result."""
    return runner.invoke(app, list(args))


def test_add_writes_the_hash(project: Tree) -> None:
    res = run("trust", "add", "wf", "--root", str(project.root))
    assert res.exit_code == 0, res.output
    assert "trusted wf" in res.output
    entries = TrustStore.load(project.root).entries
    assert len(entries) == 1
    assert entries[0].workflow == ".rayspec/workflows/wf.yaml"
    assert entries[0].hash.startswith("sha256:")


def test_add_is_idempotent_and_reports_an_update(project: Tree) -> None:
    run("trust", "add", "wf", "--root", str(project.root))
    project.workflow("wf", WF.replace("echo hi", "echo other"))
    res = run("trust", "add", "wf", "--root", str(project.root))
    assert res.exit_code == 0, res.output
    assert "updated" in res.output
    assert len(TrustStore.load(project.root).entries) == 1


def test_list_shows_the_status_of_each_entry(project: Tree) -> None:
    run("trust", "add", "wf", "--root", str(project.root))
    res = run("trust", "list", "--root", str(project.root))
    assert res.exit_code == 0, res.output
    assert "wf.yaml" in res.output
    assert "current" in res.output
    project.workflow("wf", WF.replace("echo hi", "echo drift"))
    res = run("trust", "list", "--root", str(project.root))
    assert "changed" in res.output


def test_list_json(project: Tree) -> None:
    run("trust", "add", "wf", "--root", str(project.root))
    res = run("trust", "list", "--root", str(project.root), "--json")
    assert res.exit_code == 0, res.output
    rows = json.loads(res.stdout)
    assert rows[0]["workflow"] == ".rayspec/workflows/wf.yaml"
    assert rows[0]["status"] == "current"


def test_list_on_an_empty_project_says_so(project: Tree) -> None:
    res = run("trust", "list", "--root", str(project.root))
    assert res.exit_code == 0, res.output
    assert "no trusted workflows" in res.output


def test_check_exits_1_when_a_workflow_is_not_trusted(project: Tree) -> None:
    res = run("trust", "check", "wf", "--root", str(project.root))
    assert res.exit_code == 1, res.output
    assert "not trusted" in res.output


def test_check_exits_0_when_it_is(project: Tree) -> None:
    run("trust", "add", "wf", "--root", str(project.root))
    res = run("trust", "check", "wf", "--root", str(project.root))
    assert res.exit_code == 0, res.output


def test_check_without_a_name_checks_every_discovered_workflow(project: Tree) -> None:
    project.workflow("other", WF.replace("name: wf", "name: other"))
    run("trust", "add", "wf", "--root", str(project.root))
    res = run("trust", "check", "--root", str(project.root))
    assert res.exit_code == 1, res.output
    assert "other" in res.output


def test_remove(project: Tree) -> None:
    run("trust", "add", "wf", "--root", str(project.root))
    res = run("trust", "remove", "wf", "--root", str(project.root))
    assert res.exit_code == 0, res.output
    assert TrustStore.load(project.root).entries == ()
    assert not trusted_path(project.root).exists()


def test_remove_of_an_unlisted_workflow_fails_with_a_hint(project: Tree) -> None:
    res = run("trust", "remove", "wf", "--root", str(project.root))
    assert res.exit_code == 2
    assert "rayspec trust list" in res.output


def test_add_of_an_unknown_workflow_fails(project: Tree) -> None:
    res = run("trust", "add", "nope", "--root", str(project.root))
    assert res.exit_code == 2
    assert "nope" in res.output


def test_a_run_is_refused_when_policy_requires_trust(project: Tree) -> None:
    project.policy("trust:\n  require: true\n")
    res = run("validate", "wf", "--root", str(project.root))
    assert res.exit_code == 2, res.output
    assert "rayspec trust add" in res.output
    run("trust", "add", "wf", "--root", str(project.root))
    res = run("validate", "wf", "--root", str(project.root))
    assert res.exit_code == 0, res.output


def test_trusted_file_holds_no_workflow_content(project: Tree) -> None:
    """Only a path and a digest — the trust list is committed and must carry nothing else."""
    run("trust", "add", "wf", "--root", str(project.root))
    text = Path(trusted_path(project.root)).read_text(encoding="utf-8")
    assert "echo hi" not in text
    assert set(text.split()) >= {"workflows:"}
