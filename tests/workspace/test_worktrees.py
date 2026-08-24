"""Worktree lifecycle against real temporary repositories."""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from rayspec.workspace import git as g
from rayspec.workspace.errors import WorkspaceError
from rayspec.workspace.project import project_dir, project_from_root
from rayspec.workspace.worktrees import (
    Worktree,
    clean_worktrees,
    create_worktree,
    list_worktrees,
    parse_age,
    remove_worktree,
    short_run_id,
    worktree_branch,
)

from .gitfixtures import git


def test_short_run_id_and_branch() -> None:
    assert short_run_id("20260820-101500-ab3k") == "ab3k"
    assert short_run_id("abc") == "abc"
    assert worktree_branch("fix_issue", "20260820-101500-ab3k") == "rayspec/fix_issue-ab3k"


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("7d", 7 * 86400),
        ("12h", 12 * 3600),
        ("30m", 1800),
        ("45s", 45),
        ("2w", 14 * 86400),
        ("1d12h", 86400 + 12 * 3600),
        ("0", 0),
    ],
)
def test_parse_age(text: str, seconds: int) -> None:
    assert parse_age(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["", "abc", "7x", "-1d", "1.5"])
def test_parse_age_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_age(text)


def test_create_worktree_from_current_branch(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    wt = create_worktree(
        project, home=home, workflow_name="fix_issue", run_id="20260820-101500-ab3k"
    )
    assert isinstance(wt, Worktree)
    assert wt.path == project_dir(home, project.slug) / "worktrees" / "fix_issue-ab3k"
    assert wt.path.is_dir()
    assert wt.branch == "rayspec/fix_issue-ab3k"
    assert wt.base_branch == "main"
    assert wt.base_sha == g.rev_parse(repo, "main")
    assert wt.head_sha == wt.base_sha
    assert g.current_branch(wt.path) == "rayspec/fix_issue-ab3k"
    assert (wt.path / "file1.txt").read_text() == "content 1\n"
    # the main checkout is untouched
    assert g.current_branch(repo) == "main"


def test_create_worktree_with_base_and_collision(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    git("branch", "feature", "HEAD~1", cwd=repo)
    wt = create_worktree(
        project, home=home, workflow_name="wf", run_id="20260820-101500-zzzz", base="feature"
    )
    assert wt.base_branch == "feature"
    assert wt.base_sha == g.rev_parse(repo, "feature")
    assert not (wt.path / "file1.txt").exists()
    # same short id again → falls back to the full run id, never clobbers
    wt2 = create_worktree(
        project, home=home, workflow_name="wf", run_id="20260820-101501-zzzz", base="feature"
    )
    assert wt2.branch == "rayspec/wf-20260820-101501-zzzz"
    assert wt2.path.name == "wf-20260820-101501-zzzz"


def test_create_worktree_unknown_base(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    with pytest.raises(WorkspaceError, match="nope"):
        create_worktree(project, home=home, workflow_name="wf", run_id="r1", base="nope")


def test_create_worktree_detached_head(repo: Path, home: Path) -> None:
    git("checkout", "-q", "--detach", cwd=repo)
    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-dtch")
    assert wt.base_branch is None
    assert wt.base_sha == g.rev_parse(repo)


def test_create_worktree_non_git(tmp_path: Path, home: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError, match="not a git repository"):
        create_worktree(project_from_root(plain), home=home, workflow_name="wf", run_id="r1")


def test_list_worktrees_only_rayspec_branches(repo: Path, home: Path, tmp_path: Path) -> None:
    project = project_from_root(repo)
    git("worktree", "add", "-q", "-b", "other", str(tmp_path / "other-wt"), cwd=repo)
    t0 = time.time()
    a = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-aaaa")
    b = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-bbbb")
    infos = list_worktrees(project)
    assert [i.branch for i in infos] == sorted([a.branch, b.branch])
    by_branch = {i.branch: i for i in infos}
    assert by_branch[a.branch].path == a.path
    assert by_branch[a.branch].head_sha == a.head_sha
    assert by_branch[a.branch].created_at is not None
    age = by_branch[a.branch].age
    # Bounded by the wall clock the pointer file's mtime comes from, so an `age` read off the
    # wrong epoch — the naive-local-time-labelled-UTC swap — is off by the machine's UTC offset
    # and breaks one end or the other. The 60s of slack makes the bound insensitive to the box.
    assert age is not None
    assert timedelta(0) <= age <= timedelta(seconds=time.time() - t0 + 60)
    # nothing committed yet → the worktree head equals main → counts as merged
    assert by_branch[a.branch].merged is True
    assert by_branch[a.branch].dirty is False
    (b.path / "work.txt").write_text("wip")
    git("add", ".", cwd=b.path)
    git("commit", "-q", "-m", "wip", cwd=b.path)
    (b.path / "scratch.txt").write_text("dirty")
    infos = {i.branch: i for i in list_worktrees(project)}
    assert infos[b.branch].merged is False
    assert infos[b.branch].dirty is True
    assert infos[a.branch].dirty is False


def test_remove_worktree(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-rmv1")
    remove_worktree(wt.path)
    assert not wt.path.exists()
    assert not g.branch_exists(repo, wt.branch)
    assert list_worktrees(project) == []
    # keep the branch
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-rmv2")
    remove_worktree(wt.path, delete_branch=False)
    assert not wt.path.exists()
    assert g.branch_exists(repo, wt.branch)


def test_remove_worktree_dirty_requires_force(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-drty")
    (wt.path / "scratch.txt").write_text("dirty")
    with pytest.raises(WorkspaceError):
        remove_worktree(wt.path)
    assert wt.path.exists()
    remove_worktree(wt.path, force=True)
    assert not wt.path.exists()


def test_remove_worktree_whose_directory_vanished(repo: Path, home: Path) -> None:
    import shutil

    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-gone")
    shutil.rmtree(wt.path)
    infos = list_worktrees(project)
    assert len(infos) == 1 and infos[0].prunable
    remove_worktree(wt.path, repo=repo, branch=wt.branch)
    assert list_worktrees(project) == []
    assert not g.branch_exists(repo, wt.branch)


def test_clean_worktrees(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    old = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-old1")
    new = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-new1")
    unmerged = create_worktree(
        project, home=home, workflow_name="wf", run_id="20260820-101500-unmg"
    )
    (unmerged.path / "w.txt").write_text("w")
    git("add", ".", cwd=unmerged.path)
    git("commit", "-q", "-m", "w", cwd=unmerged.path)
    # age the "old" ones by touching the .git file's mtime
    past = time.time() - 10 * 86400
    for p in (old.path, unmerged.path):
        os.utime(p / ".git", (past, past))

    report = clean_worktrees(project, older_than=timedelta(days=7), merged_only=True, dry_run=True)
    assert [w.branch for w in report.removed] == [old.branch]
    assert old.path.exists()  # dry run
    assert {w.branch for w, _ in report.skipped} == {new.branch, unmerged.branch}

    report = clean_worktrees(project, older_than=timedelta(days=7), merged_only=True)
    assert [w.branch for w in report.removed] == [old.branch]
    assert not old.path.exists()
    assert not g.branch_exists(repo, old.branch)
    assert unmerged.path.exists()

    # an unmerged worktree with commits is never removed without --force
    report = clean_worktrees(project, older_than=timedelta(days=7), merged_only=False)
    assert report.removed == []
    assert [(w.branch, r) for w, r in report.skipped if w.branch == unmerged.branch] == [
        (unmerged.branch, "unmerged commits (use --force)")
    ]
    assert unmerged.path.exists() and g.branch_exists(repo, unmerged.branch)
    report = clean_worktrees(project, older_than=timedelta(days=7), force=True)
    assert [w.branch for w in report.removed] == [unmerged.branch]

    # a dirty worktree is skipped without --force
    (new.path / "dirty.txt").write_text("x")
    report = clean_worktrees(project)
    assert report.removed == []
    assert any("dirty" in reason for _, reason in report.skipped)
    report = clean_worktrees(project, force=True)
    assert [w.branch for w in report.removed] == [new.branch]
    assert list_worktrees(project) == []


def test_recreate_worktree_from_existing_branch(repo: Path, home: Path) -> None:
    import shutil

    from rayspec.workspace.worktrees import recreate_worktree

    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-recr")
    (wt.path / "w.txt").write_text("w")
    git("add", ".", cwd=wt.path)
    git("commit", "-q", "-m", "w", cwd=wt.path)
    head = g.rev_parse(wt.path)
    shutil.rmtree(wt.path)  # simulate a deleted checkout (branch survives)
    again = recreate_worktree(project, path=wt.path, branch=wt.branch)
    assert again.path == wt.path and again.branch == wt.branch
    assert again.head_sha == head == again.base_sha
    assert (wt.path / "w.txt").exists()
    assert g.current_branch(wt.path) == wt.branch
    with pytest.raises(WorkspaceError, match="missing"):
        recreate_worktree(project, path=home / "elsewhere", branch="rayspec/missing")


def test_clean_without_filters_keeps_unmerged_work(repo: Path, home: Path) -> None:
    """A bare ``clean`` (no filters) must not delete committed-but-unmerged work."""
    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-keep")
    (wt.path / "w.txt").write_text("w")
    git("add", ".", cwd=wt.path)
    git("commit", "-q", "-m", "w", cwd=wt.path)
    report = clean_worktrees(project)
    assert report.removed == []
    assert [r for _, r in report.skipped] == ["unmerged commits (use --force)"]
    assert wt.path.exists() and g.branch_exists(repo, wt.branch)
    report = clean_worktrees(project, force=True)
    assert [w.branch for w in report.removed] == [wt.branch]
    assert not g.branch_exists(repo, wt.branch)


def test_create_worktree_repo_without_commits(tmp_path: Path, home: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    git("init", "-q", "-b", "main", cwd=empty)
    project = project_from_root(empty)
    with pytest.raises(WorkspaceError, match="no commits yet") as info:
        create_worktree(project, home=home, workflow_name="wf", run_id="r-empty")
    assert info.value.hint and "--no-worktree" in info.value.hint
    assert not isinstance(info.value, g.GitError)


def test_list_worktrees_with_dangling_merge_target(repo: Path, home: Path) -> None:
    """A dangling origin/HEAD (default branch renamed upstream) degrades to HEAD, and an
    unknown explicit merged_into is one clear error instead of per-entry git failures."""
    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-dang")
    g.run_git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/gone"], repo)
    assert g.remote_default_branch(repo) == "origin/gone"
    infos = list_worktrees(project)
    assert [i.branch for i in infos] == [wt.branch]
    assert infos[0].merged is True  # compared against HEAD (== base)
    with pytest.raises(WorkspaceError, match="nope"):
        list_worktrees(project, merged_into="nope")
    with pytest.raises(WorkspaceError, match="nope"):
        clean_worktrees(project, merged_into="nope", dry_run=True)


def test_clean_skips_locked_worktrees_and_survives_git_errors(repo: Path, home: Path) -> None:
    project = project_from_root(repo)
    locked = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-lock")
    free = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-free")
    git("worktree", "lock", str(locked.path), cwd=repo)
    infos = {i.branch: i for i in list_worktrees(project)}
    assert infos[locked.branch].locked is True and infos[free.branch].locked is False
    report = clean_worktrees(project)
    assert [w.branch for w in report.removed] == [free.branch]
    assert [(w.branch, r) for w, r in report.skipped] == [(locked.branch, "locked (use --force)")]
    assert locked.path.exists()
    # --force removes locked worktrees too (git worktree remove --force --force)
    report = clean_worktrees(project, force=True)
    assert [w.branch for w in report.removed] == [locked.branch]
    assert not locked.path.exists() and not g.branch_exists(repo, locked.branch)


def test_clean_reports_per_entry_git_failures(
    repo: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing removal is reported as skipped; the remaining candidates are still handled."""
    from rayspec.workspace import worktrees as wt_mod

    project = project_from_root(repo)
    a = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-aaaa")
    b = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-bbbb")
    real = wt_mod.remove_worktree

    def flaky(path: Path, **kwargs: object) -> None:
        if path == a.path:
            raise g.GitError("boom", returncode=128)
        real(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(wt_mod, "remove_worktree", flaky)
    report = clean_worktrees(project)
    assert [w.branch for w in report.removed] == [b.branch]
    assert [(w.branch, r) for w, r in report.skipped] == [(a.branch, "boom")]


def test_created_at_uses_oldest_pointer_mtime(repo: Path, home: Path) -> None:
    """created_at = the older of the worktree's .git pointer and git's admin gitdir file, so a
    rewritten pointer (git worktree repair/move) does not make the worktree look young."""
    project = project_from_root(repo)
    wt = create_worktree(project, home=home, workflow_name="wf", run_id="20260820-101500-ctim")
    past = time.time() - 10 * 86400
    gitdir_file = Path((wt.path / ".git").read_text().split(":", 1)[1].strip()) / "gitdir"
    assert gitdir_file.is_file()
    os.utime(gitdir_file, (past, past))  # only the admin file is old
    info = list_worktrees(project)[0]
    assert info.age is not None and info.age >= timedelta(days=9)
