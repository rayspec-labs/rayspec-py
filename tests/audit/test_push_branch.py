"""The push hook: publish a run's branch, and never let a failed push change the run."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.workspace.git import (
    PUSH_ENV,
    PushOutcome,
    branch_exists,
    push_branch,
    push_remote,
    run_git,
)

from .conftest import git


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repository to push into."""
    path = tmp_path / "origin.git"
    path.mkdir()
    git("init", "-q", "--bare", "-b", "main", cwd=path)
    return path


@pytest.fixture
def clone(repo: Path, origin: Path) -> Path:
    git("remote", "add", "origin", str(origin), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    git("checkout", "-q", "-b", "rayspec/work-abcd", cwd=repo)
    (repo / "b.txt").write_text("b\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "work", cwd=repo)
    return repo


def _remote_branches(origin: Path) -> list[str]:
    out = run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], origin)
    return out.stdout.split()


def test_pushes_the_branch(clone: Path, origin: Path) -> None:
    outcome = push_branch(clone, "rayspec/work-abcd")
    assert isinstance(outcome, PushOutcome)
    assert outcome.pushed is True and outcome.reason is None
    assert outcome.branch == "rayspec/work-abcd" and outcome.remote == "origin"
    assert "rayspec/work-abcd" in _remote_branches(origin)


def test_an_existing_remote_branch_fast_forwards(clone: Path, origin: Path) -> None:
    assert push_branch(clone, "rayspec/work-abcd").pushed
    (clone / "c.txt").write_text("c\n")
    git("add", ".", cwd=clone)
    git("commit", "-q", "-m", "more", cwd=clone)
    assert push_branch(clone, "rayspec/work-abcd").pushed


def test_a_diverged_remote_branch_is_a_reason_not_an_exception(clone: Path, origin: Path) -> None:
    assert push_branch(clone, "rayspec/work-abcd").pushed
    # somebody else moved the remote branch on; rayspec must not force over it
    other = clone.parent / "other"
    git("clone", "-q", str(origin), str(other), cwd=clone.parent)
    git("checkout", "-q", "rayspec/work-abcd", cwd=other)
    (other / "theirs.txt").write_text("theirs\n")
    git("add", ".", cwd=other)
    git(
        "-c",
        "user.email=o@x.invalid",
        "-c",
        "user.name=O",
        "commit",
        "-q",
        "-m",
        "theirs",
        cwd=other,
    )
    git("push", "-q", "origin", "rayspec/work-abcd", cwd=other)
    git("reset", "-q", "--hard", "HEAD~1", cwd=clone)
    (clone / "mine.txt").write_text("mine\n")
    git("add", ".", cwd=clone)
    git("commit", "-q", "-m", "mine", cwd=clone)
    outcome = push_branch(clone, "rayspec/work-abcd")
    assert outcome.pushed is False
    assert outcome.reason and "rejected" in outcome.reason.lower()


def test_no_remote_is_a_reason(repo: Path) -> None:
    outcome = push_branch(repo, "main")
    assert outcome.pushed is False
    assert outcome.reason and "origin" in outcome.reason


def test_an_unknown_branch_is_a_reason(clone: Path) -> None:
    assert not branch_exists(clone, "rayspec/nope")
    outcome = push_branch(clone, "rayspec/nope")
    assert outcome.pushed is False and outcome.reason


def test_a_missing_git_binary_is_a_reason(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    outcome = push_branch(clone, "rayspec/work-abcd")
    assert outcome.pushed is False and outcome.reason


def test_push_remote_reads_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    assert push_remote() is None
    monkeypatch.setenv(PUSH_ENV, "1")
    assert push_remote() == "origin"
    monkeypatch.setenv(PUSH_ENV, "upstream")
    assert push_remote() == "upstream"
    monkeypatch.setenv(PUSH_ENV, "0")
    assert push_remote() is None
    monkeypatch.setenv(PUSH_ENV, "  ")
    assert push_remote() is None


def test_uncommitted_work_is_counted_not_published(clone: Path, origin: Path) -> None:
    # rayspec never commits: a workflow that leaves work in the worktree publishes nothing of it
    (clone / "note.txt").write_text("not committed\n")
    outcome = push_branch(clone, "rayspec/work-abcd")
    assert outcome.pushed is True
    assert outcome.uncommitted == 1
    shown = run_git(["show", "rayspec/work-abcd:note.txt"], origin, check=False)
    assert not shown.ok, "only committed work can reach the remote"


def test_a_clean_worktree_counts_nothing(clone: Path) -> None:
    outcome = push_branch(clone, "rayspec/work-abcd")
    assert outcome.pushed is True and outcome.uncommitted == 0


def test_the_push_leaves_no_upstream_config_behind(clone: Path) -> None:
    assert push_branch(clone, "rayspec/work-abcd").pushed
    config = run_git(["config", "--get", "branch.rayspec/work-abcd.remote"], clone, check=False)
    assert not config.ok, "a throwaway branch must not add entries to the repository's config"


def test_the_push_cannot_ask_for_a_passphrase(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import Mapping, Sequence

    from rayspec.workspace import git as gitmod

    seen: dict[str, str] = {}
    original = gitmod.run_git

    def spy(
        args: Sequence[str],
        cwd: Path | str | None,
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> gitmod.GitResult:
        if args and args[0] == "push":
            seen.update(env or {})
        return original(args, cwd, check=check, env=env, timeout=timeout)

    monkeypatch.setattr(gitmod, "run_git", spy)
    assert push_branch(clone, "rayspec/work-abcd").pushed
    # the hook runs while a run is finishing: a locked ssh key must fail, not stall it for a
    # minute or pop a dialog on somebody's desktop
    assert "BatchMode=yes" in seen.get("GIT_SSH_COMMAND", "")
    assert seen.get("SSH_ASKPASS_REQUIRE") == "never"
    assert seen.get("GIT_ASKPASS")
