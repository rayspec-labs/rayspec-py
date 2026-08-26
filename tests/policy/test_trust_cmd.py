"""``rayspec trust add|list|remove|check`` — the CLI over ``.rayspec/trusted.yaml``."""

from __future__ import annotations

import json
import os
import stat
import sys
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


def test_list_uses_the_same_comparison_as_the_gate(project: Tree) -> None:
    """`trust list` is read to predict `rayspec run`; a looser comparison would mislead."""
    run("trust", "add", "wf", "--root", str(project.root))
    path = trusted_path(project.root)
    digest = TrustStore.load(project.root).entries[0].hash.split(":", 1)[1]
    path.write_text(
        f"workflows:\n- workflow: .rayspec/workflows/wf.yaml\n  hash: md5:{digest}\n",
        encoding="utf-8",
    )
    res = run("trust", "list", "--root", str(project.root))
    assert "changed" in res.output
    res = run("trust", "check", "wf", "--root", str(project.root))
    assert res.exit_code == 1, res.output


def test_check_reports_every_workflow_even_when_one_does_not_load(project: Tree) -> None:
    """One unparsable file must not hide the trust status of the rest of the repository."""
    project.workflow("aaa", WF.replace("name: wf", "name: aaa"))
    project.workflow("zzz", "rayspec: 1\nname: [oops\n")
    res = run("trust", "check", "--root", str(project.root))
    assert res.exit_code == 1, res.output
    assert "aaa" in res.output
    assert "zzz" in res.output
    assert "does not load" in res.output


def test_check_of_a_named_target_that_does_not_exist_is_exit_2(project: Tree) -> None:
    res = run("trust", "check", "nope", "--root", str(project.root))
    assert res.exit_code == 2, res.output


def test_check_json_carries_the_load_failure(project: Tree) -> None:
    project.workflow("zzz", "rayspec: 1\nname: [oops\n")
    res = run("trust", "check", "--root", str(project.root), "--json")
    assert res.exit_code == 1
    rows = {row["workflow"]: row for row in json.loads(res.stdout)}
    broken = next(row for name, row in rows.items() if "zzz" in name)
    assert broken["trusted"] is False
    assert "does not load" in broken["problem"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_trust_file_is_readable_by_everyone_in_the_checkout(project: Tree) -> None:
    """It is committed to the repository and holds no secret; a 0600 file breaks a shared box."""
    before = os.umask(0o077)
    try:
        run("trust", "add", "wf", "--root", str(project.root))
    finally:
        assert os.umask(before) == 0o077, "saving the trust list changed the process umask"
    assert stat.S_IMODE(trusted_path(project.root).stat().st_mode) == 0o644


def test_check_without_a_name_ignores_the_bundled_library(project: Tree) -> None:
    """A scheduled `trust check` must not go red because an upgrade shipped new workflows."""
    run("trust", "add", "wf", "--root", str(project.root))
    res = run("trust", "check", "--root", str(project.root))
    assert res.exit_code == 0, res.output
    assert "pr_review" not in res.output


def test_a_bundled_workflow_is_trusted_by_its_stable_label(project: Tree) -> None:
    res = run("trust", "add", "pr_review", "--root", str(project.root))
    assert res.exit_code == 0, res.output
    text = trusted_path(project.root).read_text(encoding="utf-8")
    assert "workflow: <bundled>/pr_review.yaml" in text
    res = run("trust", "check", "pr_review", "--root", str(project.root))
    assert res.exit_code == 0, res.output
    res = run("trust", "list", "--json", "--root", str(project.root))
    (row,) = json.loads(res.stdout)
    assert row["workflow"] == "<bundled>/pr_review.yaml" and row["status"] == "current"
