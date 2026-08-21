"""prepare_workspace: the single entry point used by the engine / CLI."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from rayspec.config import Config, ProjectSpec
from rayspec.workspace import (
    PathLock,
    Workspace,
    WorkspaceError,
    prepare_workspace,
    prepare_workspace_async,
    workspace_lock,
)
from rayspec.workspace import git as g
from rayspec.workspace.project import project_dir

from .gitfixtures import git, make_repo

pytestmark = pytest.mark.anyio


def test_workspace_field_names_match_engine_contract() -> None:
    names = [f.name for f in fields(Workspace)]
    for required in (
        "isolation",
        "project_root",
        "workdir",
        "branch",
        "base_branch",
        "base_sha",
        "head_sha",
    ):
        assert required in names


def test_prepare_worktree_default(repo: Path, home: Path) -> None:
    ws = prepare_workspace(
        repo, home=home, workflow_name="fix_issue", run_id="20260820-101500-ab3k"
    )
    assert ws.isolation == "worktree"
    assert ws.project_root == repo.resolve()
    assert (
        ws.workdir == project_dir(home, "github.com/Acme/Widget") / "worktrees" / "fix_issue-ab3k"
    )
    assert ws.branch == "rayspec/fix_issue-ab3k"
    assert ws.base_branch == "main"
    assert ws.base_sha == g.rev_parse(repo) == ws.head_sha
    assert ws.slug == "github.com/Acme/Widget"
    assert ws.notice is None
    assert ws.source_root is None
    assert g.current_branch(ws.workdir) == ws.branch


def test_prepare_worktree_with_base(repo: Path, home: Path) -> None:
    git("branch", "release", "HEAD~1", cwd=repo)
    ws = prepare_workspace(repo, home=home, workflow_name="wf", run_id="r-1234", base="release")
    assert ws.base_branch == "release"
    assert ws.base_sha == g.rev_parse(repo, "release")


def test_prepare_isolation_none(repo: Path, home: Path) -> None:
    ws = prepare_workspace(repo, home=home, workflow_name="wf", run_id="r-1", isolation="none")
    assert ws.isolation == "none"
    assert ws.workdir == repo.resolve() == ws.project_root
    assert ws.branch == "main"
    assert ws.head_sha == g.rev_parse(repo)
    assert ws.base_branch is None and ws.base_sha is None
    assert ws.notice is None


def test_prepare_non_git_runs_in_place_with_notice(tmp_path: Path, home: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    ws = prepare_workspace(plain, home=home, workflow_name="wf", run_id="r-1")
    assert ws.isolation == "none"
    assert ws.workdir == plain.resolve()
    assert ws.branch is None and ws.head_sha is None
    assert ws.notice and "not a git repository" in ws.notice
    assert ws.slug.startswith("local/plain-")


def test_prepare_bad_isolation(repo: Path, home: Path) -> None:
    with pytest.raises(WorkspaceError, match="isolation"):
        prepare_workspace(repo, home=home, workflow_name="wf", run_id="r", isolation="container")  # type: ignore[arg-type]


def test_prepare_repo_url_always_worktree(tmp_path: Path, home: Path) -> None:
    upstream = make_repo(tmp_path / "upstream", commits=2)
    url = upstream.resolve().as_uri()
    ws = prepare_workspace(
        tmp_path,
        home=home,
        workflow_name="wf",
        run_id="20260820-101500-url1",
        isolation="none",
        repo_arg=url,
    )
    assert ws.isolation == "worktree"
    assert ws.source_root is not None and ws.source_root.name == "source.git"
    assert ws.workdir == project_dir(home, ws.slug) / "worktrees" / "wf-url1"
    assert ws.project_root == ws.workdir  # workflows are loaded from the checkout
    assert ws.base_branch == "origin/main"
    assert ws.base_sha == g.rev_parse(upstream, "main")
    assert (ws.workdir / "file1.txt").exists()
    assert ws.notice and "worktree" in ws.notice


def test_prepare_repo_path_and_registered_name(tmp_path: Path, repo: Path, home: Path) -> None:
    ws = prepare_workspace(
        tmp_path, home=home, workflow_name="wf", run_id="r-path", repo_arg=str(repo)
    )
    assert ws.project_root == repo.resolve()
    assert ws.isolation == "worktree"
    git("branch", "develop", "HEAD~1", cwd=repo)
    config = Config(projects=[ProjectSpec(name="widget", source=str(repo), base="develop")])
    ws2 = prepare_workspace(
        tmp_path, home=home, workflow_name="wf", run_id="r-name", repo_arg="widget", config=config
    )
    assert ws2.project_root == repo.resolve()
    assert ws2.base_branch == "develop"
    # explicit --base beats the registered base
    ws3 = prepare_workspace(
        tmp_path,
        home=home,
        workflow_name="wf",
        run_id="r-name2",
        repo_arg="widget",
        config=config,
        base="main",
    )
    assert ws3.base_branch == "main"


def test_workspace_lock_helper(repo: Path, home: Path) -> None:
    ws = prepare_workspace(repo, home=home, workflow_name="wf", run_id="r-lock", isolation="none")
    lock = workspace_lock(ws, home=home, run_id="r-lock")
    assert isinstance(lock, PathLock)
    assert lock.path.parent == project_dir(home, ws.slug) / "locks"
    with lock:
        assert lock.held


async def test_prepare_workspace_async(repo: Path, home: Path) -> None:
    ws = await prepare_workspace_async(
        repo, home=home, workflow_name="wf", run_id="r-async", isolation="none"
    )
    assert ws.workdir == repo.resolve()


def test_workspace_isolation_is_literal() -> None:
    from typing import get_type_hints

    from rayspec.workspace.prepare import Isolation

    hints = get_type_hints(Workspace)
    assert hints["isolation"] == Isolation


def test_prepare_isolation_none_ignores_base_with_notice(repo: Path, home: Path) -> None:
    ws = prepare_workspace(
        repo, home=home, workflow_name="wf", run_id="r-nb", isolation="none", base="main"
    )
    assert ws.isolation == "none" and ws.workdir == repo.resolve()
    assert ws.base_branch is None and ws.base_sha is None
    assert ws.notice is not None and "main" in ws.notice and "ignored" in ws.notice


def test_prepare_in_place_on_repo_without_commits(tmp_path: Path, home: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    git("init", "-q", "-b", "main", cwd=empty)
    with pytest.raises(WorkspaceError, match="no commits yet"):
        prepare_workspace(empty, home=home, workflow_name="wf", run_id="r-e1")
    ws = prepare_workspace(empty, home=home, workflow_name="wf", run_id="r-e2", isolation="none")
    assert ws.isolation == "none" and ws.head_sha is None and ws.branch == "main"


def test_prepare_worktree_for_a_project_below_the_git_toplevel(repo: Path, home: Path) -> None:
    """``packages/foo/.rayspec`` in a monorepo: steps must run in ``<worktree>/packages/foo``."""
    sub = repo / "packages" / "foo"
    (sub / ".rayspec" / "workflows").mkdir(parents=True)
    (sub / "hello.txt").write_text("hi\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "add packages/foo", cwd=repo)
    ws = prepare_workspace(sub, home=home, workflow_name="wf", run_id="20260820-101500-sub1")
    assert ws.isolation == "worktree"
    assert ws.project_root == sub.resolve()
    wt_root = project_dir(home, "github.com/Acme/Widget") / "worktrees" / "wf-sub1"
    assert ws.workdir == wt_root / "packages" / "foo"
    assert (ws.workdir / "hello.txt").is_file()
    assert g.current_branch(ws.workdir) == ws.branch
