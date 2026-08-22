"""The worktree change guard, measured against a real git repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.workspace import GitError, check_change_guard, diff_since, match_path
from workspace.gitfixtures import GIT_ENV, git, make_repo


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "repo", commits=1)


def base_of(repo: Path) -> str:
    return git("rev-parse", "HEAD", cwd=repo)


def test_a_clean_worktree_changes_nothing(repo: Path) -> None:
    report = check_change_guard(repo, base_of(repo), max_changed_files=0)
    assert report.ok
    assert report.summary.changed_files == 0
    assert report.summary.changed_lines == 0


def test_tracked_edits_are_counted(repo: Path) -> None:
    (repo / "file0.txt").write_text("a\nb\nc\n", encoding="utf-8")
    summary = diff_since(repo, base_of(repo))
    assert [f.path for f in summary.files] == ["file0.txt"]
    assert summary.changed_files == 1
    assert summary.changed_lines == 4  # one line replaced by three


def test_untracked_files_count_too(repo: Path) -> None:
    (repo / "new.txt").write_text("one\ntwo\n", encoding="utf-8")
    summary = diff_since(repo, base_of(repo))
    assert [f.path for f in summary.files] == ["new.txt"]
    assert summary.files[0].untracked
    assert summary.changed_lines == 2


def test_max_changed_files_trips_and_says_what_exceeded_it(repo: Path) -> None:
    for n in range(4):
        (repo / f"extra{n}.txt").write_text("x\n", encoding="utf-8")
    report = check_change_guard(repo, base_of(repo), max_changed_files=2)
    assert not report.ok
    assert [v.kind for v in report.violations] == ["max_changed_files"]
    assert "max_changed_files: 4 files changed, limit 2" in report.message
    assert "extra0.txt" in report.message


def test_max_changed_lines_trips(repo: Path) -> None:
    (repo / "big.txt").write_text("line\n" * 40, encoding="utf-8")
    report = check_change_guard(repo, base_of(repo), max_changed_lines=10)
    assert [v.kind for v in report.violations] == ["max_changed_lines"]
    assert "max_changed_lines: 40 lines changed, limit 10" in report.message


def test_a_protected_path_trips_and_names_the_glob(repo: Path) -> None:
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    report = check_change_guard(repo, base_of(repo), protected_paths=[".github/**"])
    assert [v.kind for v in report.violations] == ["protected_path"]
    assert ".github/workflows/ci.yml" in report.message
    assert ".github/**" in report.message


def test_a_deleted_protected_file_trips_too(repo: Path) -> None:
    (repo / "file0.txt").unlink()
    report = check_change_guard(repo, base_of(repo), protected_paths=["file0.txt"])
    assert not report.ok


def test_every_broken_limit_is_reported_not_just_the_first(repo: Path) -> None:
    (repo / "infra").mkdir()
    for n in range(3):
        (repo / "infra" / f"f{n}.tf").write_text("resource {}\n" * 5, encoding="utf-8")
    report = check_change_guard(
        repo,
        base_of(repo),
        protected_paths=["infra/**"],
        max_changed_files=1,
        max_changed_lines=2,
    )
    assert sorted({v.kind for v in report.violations}) == [
        "max_changed_files",
        "max_changed_lines",
        "protected_path",
    ]


def test_binary_files_count_as_a_changed_file_but_no_lines(repo: Path) -> None:
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    summary = diff_since(repo, base_of(repo))
    assert summary.files[0].binary
    assert summary.changed_files == 1
    assert summary.changed_lines == 0


def test_an_unknown_base_is_a_git_error(repo: Path) -> None:
    with pytest.raises(GitError):
        diff_since(repo, "0" * 40)


def test_a_non_git_directory_is_a_git_error(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        diff_since(tmp_path, "HEAD")


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        (".github/workflows/ci.yml", ".github/**", True),
        (".github/workflows/ci.yml", ".github/", True),
        ("uv.lock", "**/*.lock", True),
        ("sub/uv.lock", "**/*.lock", True),
        ("uv.lock", "*.lock", True),
        ("src/app.py", "*.lock", False),
        ("infra/main.tf", "infra/**", True),
        ("infrastructure/main.tf", "infra/**", False),
    ],
)
def test_glob_matching(path: str, pattern: str, expected: bool) -> None:
    assert match_path(path, pattern) is expected


def test_a_rename_out_of_a_protected_directory_trips(repo: Path) -> None:
    """`git mv` of a protected file is a change to it — the strongest claim the guard makes."""
    (repo / ".github").mkdir()
    (repo / ".github" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "ci", cwd=repo)
    base = base_of(repo)
    git("mv", ".github/ci.yml", "ci.yml", cwd=repo)
    report = check_change_guard(repo, base, protected_paths=[".github/**"])
    assert not report.ok
    assert ".github/ci.yml" in report.message


def test_a_rename_never_produces_an_empty_path(repo: Path) -> None:
    git("mv", "file0.txt", "renamed.txt", cwd=repo)
    summary = diff_since(repo, base_of(repo))
    assert "" not in [f.path for f in summary.files]
    assert sorted(f.path for f in summary.files) == ["file0.txt", "renamed.txt"]
    assert summary.changed_files == 2


def test_a_rename_with_edits_keeps_both_paths(repo: Path) -> None:
    git("mv", "file0.txt", "moved.txt", cwd=repo)
    (repo / "moved.txt").write_text("a\nb\nc\n", encoding="utf-8")
    summary = diff_since(repo, base_of(repo))
    assert sorted(f.path for f in summary.files) == ["file0.txt", "moved.txt"]


def test_files_in_a_gitignored_directory_are_counted(repo: Path) -> None:
    """`--exclude-standard` hides exactly the paths an agent is most likely to write into."""
    (repo / ".gitignore").write_text("secrets/\n.env\n", encoding="utf-8")
    git("add", ".gitignore", cwd=repo)
    git("commit", "-q", "-m", "ignore", cwd=repo)
    base = base_of(repo)
    (repo / "secrets").mkdir()
    (repo / "secrets" / "key.pem").write_text("k\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    summary = diff_since(repo, base)
    assert sorted(f.path for f in summary.files) == [".env", "secrets/key.pem"]
    report = check_change_guard(repo, base, protected_paths=["**/.env"])
    assert not report.ok
    assert ".env" in report.message


def test_ignored_files_can_be_left_out(repo: Path) -> None:
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    git("add", ".gitignore", cwd=repo)
    git("commit", "-q", "-m", "ignore", cwd=repo)
    (repo / "build").mkdir()
    (repo / "build" / "out.js").write_text("x\n", encoding="utf-8")
    summary = diff_since(repo, base_of(repo), include_ignored=False)
    assert [f.path for f in summary.files] == []


def test_the_git_directory_is_never_counted(repo: Path) -> None:
    summary = diff_since(repo, base_of(repo))
    assert not [f for f in summary.files if f.path.startswith(".git/")]
