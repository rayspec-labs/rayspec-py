"""Thin git helpers against real temporary repositories."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.errors import RayspecError
from rayspec.workspace import git as g
from rayspec.workspace.errors import GitError

from .gitfixtures import git, make_repo


def test_run_git_captures_errors(repo: Path) -> None:
    with pytest.raises(GitError) as exc:
        g.run_git(["rev-parse", "--verify", "nope^{commit}"], repo)
    assert isinstance(exc.value, RayspecError)
    assert exc.value.returncode not in (None, 0)
    assert "rev-parse" in str(exc.value)
    res = g.run_git(["rev-parse", "--verify", "nope^{commit}"], repo, check=False)
    assert not res.ok


def test_rev_parse_and_branch(repo: Path) -> None:
    sha = g.rev_parse(repo)
    assert len(sha) == 40
    assert g.rev_parse(repo, "main") == sha
    assert g.current_branch(repo) == "main"
    assert g.branch_exists(repo, "main")
    assert not g.branch_exists(repo, "rayspec/x")
    with pytest.raises(GitError):
        g.rev_parse(repo, "does-not-exist")
    git("checkout", "-q", "--detach", cwd=repo)
    assert g.current_branch(repo) is None


def test_is_dirty(repo: Path) -> None:
    assert not g.is_dirty(repo)
    (repo / "new.txt").write_text("x")
    assert g.is_dirty(repo)
    assert not g.is_dirty(repo, untracked=False)
    (repo / "file0.txt").write_text("changed")
    assert g.is_dirty(repo, untracked=False)


def test_toplevel_and_is_git_repo(repo: Path, tmp_path: Path) -> None:
    sub = repo / "deep"
    sub.mkdir()
    assert g.toplevel(sub) == repo.resolve()
    assert g.is_git_repo(repo)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert g.toplevel(plain) is None
    assert not g.is_git_repo(plain)
    assert not g.is_git_repo(tmp_path / "missing")


def test_remote_default_branch_and_fetch(repo: Path, tmp_path: Path) -> None:
    upstream = make_repo(tmp_path / "upstream", commits=1)
    clone = tmp_path / "clone"
    git("clone", "-q", str(upstream), str(clone), cwd=tmp_path)
    assert g.remote_url(clone) == str(upstream)
    assert g.remote_default_branch(clone) == "origin/main"
    assert g.remote_url(repo) == "git@github.com:Acme/Widget.git"
    assert g.remote_default_branch(repo) is None  # never fetched
    (upstream / "more.txt").write_text("more")
    git("add", ".", cwd=upstream)
    git("commit", "-q", "-m", "more", cwd=upstream)
    g.fetch_prune(clone)
    assert g.rev_parse(clone, "origin/main") == g.rev_parse(upstream, "main")
    assert g.is_ancestor(clone, "HEAD", "origin/main")
    assert not g.is_ancestor(clone, "origin/main", "HEAD")
