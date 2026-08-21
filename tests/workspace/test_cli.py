"""``rayspec worktrees`` and ``rayspec projects`` via CliRunner (RAYSPEC_HOME → temp dir)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import yaml
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.workspace import git as g
from rayspec.workspace.project import project_from_root
from rayspec.workspace.registry import add_project, list_projects, remove_project
from rayspec.workspace.worktrees import create_worktree, list_worktrees

from .gitfixtures import git, make_repo

runner = CliRunner()


def test_worktrees_list_empty(repo: Path, home: Path) -> None:
    result = runner.invoke(app, ["worktrees", "list", "--root", str(repo)])
    assert result.exit_code == 0, result.output
    assert "no rayspec worktrees" in result.output
    result = runner.invoke(app, ["worktrees", "list", "--root", str(repo), "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_worktrees_list_and_json(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-abcd")
    result = runner.invoke(app, ["worktrees", "list", "--root", str(repo)])
    assert result.exit_code == 0, result.output
    assert "rayspec/wf-abcd" in result.output
    assert "merged" in result.output
    result = runner.invoke(app, ["worktrees", "list", "--root", str(repo), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data) == 1
    assert data[0]["branch"] == wt.branch
    assert data[0]["path"] == str(wt.path)
    assert data[0]["merged"] is True
    assert data[0]["dirty"] is False
    assert data[0]["head_sha"] == wt.head_sha
    assert "age_s" in data[0] and "created_at" in data[0]


def test_worktrees_clean(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    old = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-old1")
    new = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-new1")
    past = time.time() - 10 * 86400
    os.utime(old.path / ".git", (past, past))

    result = runner.invoke(
        app,
        ["worktrees", "clean", "--root", str(repo), "--older-than", "7d", "--merged", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "would remove" in result.output and old.branch in result.output
    assert old.path.exists()

    result = runner.invoke(
        app, ["worktrees", "clean", "--root", str(repo), "--older-than", "7d", "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [w["branch"] for w in data["removed"]] == [old.branch]
    assert [s["branch"] for s in data["skipped"]] == [new.branch]
    assert not old.path.exists()
    assert not g.branch_exists(repo, old.branch)

    (new.path / "dirty.txt").write_text("x")
    result = runner.invoke(app, ["worktrees", "clean", "--root", str(repo)])
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output and "dirty" in result.output
    assert new.path.exists()
    result = runner.invoke(app, ["worktrees", "clean", "--root", str(repo), "--force"])
    assert result.exit_code == 0, result.output
    assert "removed" in result.output
    assert list_worktrees(project) == []


def test_worktrees_clean_bad_age(repo: Path, home: Path) -> None:
    result = runner.invoke(app, ["worktrees", "clean", "--root", str(repo), "--older-than", "soon"])
    assert result.exit_code == 2
    assert "invalid age" in result.output


def test_worktrees_outside_git(tmp_path: Path, home: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = runner.invoke(app, ["worktrees", "list", "--root", str(plain)])
    assert result.exit_code == 2
    assert "not a git repository" in result.output


def test_projects_add_list_remove(repo: Path, home: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["projects", "list"])
    assert result.exit_code == 0, result.output
    assert "no registered projects" in result.output

    result = runner.invoke(app, ["projects", "add", "widget", str(repo), "--base", "main"])
    assert result.exit_code == 0, result.output
    assert "registered project widget" in result.output
    data = yaml.safe_load((home / "config.yaml").read_text())
    assert data["projects"] == [{"name": "widget", "source": str(repo.resolve()), "base": "main"}]

    result = runner.invoke(app, ["projects", "add", "remote", "git@github.com:o/r.git"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["projects", "list"])
    assert result.exit_code == 0, result.output
    assert (
        "widget" in result.output
        and "remote" in result.output
        and "git@github.com:o/r.git" in result.output
    )
    result = runner.invoke(app, ["projects", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {"name": "remote", "source": "git@github.com:o/r.git", "base": None},
        {"name": "widget", "source": str(repo.resolve()), "base": "main"},
    ]

    # re-adding updates in place
    result = runner.invoke(app, ["projects", "add", "widget", str(repo), "--base", "develop"])
    assert result.exit_code == 0, result.output
    assert "updated project widget" in result.output
    assert [p.base for p in list_projects(home) if p.name == "widget"] == ["develop"]

    result = runner.invoke(app, ["projects", "remove", "widget"])
    assert result.exit_code == 0, result.output
    assert "removed project widget" in result.output
    assert [p.name for p in list_projects(home)] == ["remote"]
    result = runner.invoke(app, ["projects", "remove", "widget"])
    assert result.exit_code == 2
    assert "no registered project" in result.output


def test_projects_add_validation(home: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["projects", "add", "bad", str(tmp_path / "missing")])
    assert result.exit_code == 2
    assert "not a directory" in result.output or "git URL" in result.output
    result = runner.invoke(app, ["projects", "add", "bad name", "git@github.com:o/r.git"])
    assert result.exit_code == 2
    assert "name" in result.output


def test_registry_preserves_other_config_keys(home: Path) -> None:
    (home / "config.yaml").write_text(
        "default_provider: codex\naliases:\n  '@mini': {model: gpt-5.4}\n"
    )
    add_project(home, "p", "git@github.com:o/r.git", base=None)
    data = yaml.safe_load((home / "config.yaml").read_text())
    assert data["default_provider"] == "codex"
    assert data["aliases"] == {"@mini": {"model": "gpt-5.4"}}
    assert data["projects"] == [{"name": "p", "source": "git@github.com:o/r.git"}]
    assert remove_project(home, "p") is True
    assert remove_project(home, "p") is False
    assert yaml.safe_load((home / "config.yaml").read_text())["projects"] == []


def test_projects_roundtrip_with_config_loader(repo: Path, home: Path) -> None:
    from rayspec.config import load_config

    add_project(home, "widget", str(repo), base="main")
    cfg = load_config(repo, home=home)
    assert cfg.projects[0].name == "widget" and cfg.projects[0].base == "main"


def test_worktrees_list_for_registered_url_source(tmp_path: Path, home: Path) -> None:
    """worktrees --repo <name> lists the worktrees of a registered (bare) source."""
    upstream = make_repo(tmp_path / "upstream")
    url = upstream.resolve().as_uri()
    add_project(home, "up", url, base=None)
    from rayspec.workspace import prepare_workspace
    from rayspec.workspace.registry import list_projects as _lp

    assert [p.name for p in _lp(home)] == ["up"]
    ws = prepare_workspace(
        tmp_path, home=home, workflow_name="wf", run_id="20260820-101500-upup", repo_arg=url
    )
    result = runner.invoke(app, ["worktrees", "list", "--repo", "up", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [w["branch"] for w in data] == [ws.branch]
    git("status", cwd=ws.workdir)


def test_worktrees_clean_unknown_merge_target(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-mrgt")
    result = runner.invoke(
        app, ["worktrees", "clean", "--root", str(repo), "--merged-into", "nope", "--dry-run"]
    )
    assert result.exit_code == 2, result.output
    assert "nope" in result.output and "--merged-into" in result.output


def test_worktrees_clean_removes_the_lock_files_of_removed_worktrees(
    repo: Path, home: Path
) -> None:
    """The per-workdir lock file of a removed worktree goes with it (unless it is held)."""
    from rayspec.workspace.lock import PathLock, lock_path

    project = project_from_root(repo)
    gone = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-gone")
    held = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-held")
    PathLock(home, project.slug, gone.path, run_id="r1").acquire().release()  # released: stale
    keeper = PathLock(home, project.slug, held.path, run_id="r2").acquire()  # still held
    gone_lock = lock_path(home, project.slug, gone.path)
    held_lock = lock_path(home, project.slug, held.path)
    assert gone_lock.exists() and held_lock.exists()
    result = runner.invoke(app, ["worktrees", "clean", "--root", str(repo), "--force"])
    assert result.exit_code == 0, result.output
    assert not gone.path.exists() and not held.path.exists()
    assert not gone_lock.exists(), "stale lock file removed with its worktree"
    assert held_lock.exists(), "a held lock is never unlinked"
    keeper.release()
