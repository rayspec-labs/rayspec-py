"""Project discovery + slug normalisation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from rayspec.workspace.project import (
    Project,
    discover_project,
    normalize_remote_url,
    project_dir,
    project_from_root,
    project_slug,
)

from .gitfixtures import git, make_repo


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:owner/repo.git", "github.com/owner/repo"),
        ("https://github.com/own%20er/re%2Bpo.git", "github.com/own er/re+po"),
        ("git@github.com:owner/repo", "github.com/owner/repo"),
        ("git@GitHub.com:Owner/Repo.git", "github.com/Owner/Repo"),
        ("ssh://git@github.com/owner/repo.git", "github.com/owner/repo"),
        ("ssh://git@github.com:2222/owner/repo", "github.com/owner/repo"),
        ("ssh://github.com/owner/repo/", "github.com/owner/repo"),
        ("https://github.com/owner/repo.git", "github.com/owner/repo"),
        ("https://github.com/owner/repo", "github.com/owner/repo"),
        (
            "https://user:token@gitlab.example.com/group/sub/repo.git",
            "gitlab.example.com/group/sub/repo",
        ),
        ("http://HOST.example/o/r.git/", "host.example/o/r"),
        ("git://github.com/owner/repo.git", "github.com/owner/repo"),
        ("https://dev.azure.com/org/project/_git/repo", "dev.azure.com/org/project/_git/repo"),
    ],
)
def test_normalize_remote_url(url: str, slug: str) -> None:
    assert normalize_remote_url(url) == slug


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "file:///tmp/x/repo.git",
        "/abs/path/repo",
        "../relative",
        "nonsense",
        "https://host",
    ],
)
def test_normalize_remote_url_rejects_non_remote(url: str) -> None:
    assert normalize_remote_url(url) is None


def test_project_slug_from_origin(repo: Path) -> None:
    assert project_slug(repo) == "github.com/Acme/Widget"


def test_project_slug_fallback_without_remote(tmp_path: Path) -> None:
    r = make_repo(tmp_path / "lonely")
    slug = project_slug(r)
    digest = hashlib.sha1(str(r.resolve()).encode()).hexdigest()[:8]
    assert slug == f"local/lonely-{digest}"


def test_project_slug_fallback_for_local_file_remote(tmp_path: Path) -> None:
    r = make_repo(tmp_path / "child")
    git("remote", "add", "origin", "file:///somewhere/else.git", cwd=r)
    assert project_slug(r).startswith("local/child-")


def test_project_slug_non_git(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    assert project_slug(d).startswith("local/plain-")


def test_discover_project_uses_the_git_top_level(repo: Path) -> None:
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert discover_project(sub).root == repo.resolve()


def test_discover_project_without_git_uses_the_directory(tmp_path: Path) -> None:
    d = tmp_path / "plain" / "deep"
    d.mkdir(parents=True)
    assert discover_project(d).root == d.resolve()


def test_project_from_root(repo: Path, tmp_path: Path) -> None:
    p = project_from_root(repo)
    assert p == Project(
        root=repo.resolve(), slug="github.com/Acme/Widget", name="Widget", is_git=True
    )
    plain = tmp_path / "plain"
    plain.mkdir()
    q = project_from_root(plain)
    assert q.is_git is False
    assert q.name == "plain"
    assert q.slug.startswith("local/plain-")


def test_project_dir(tmp_path: Path) -> None:
    assert (
        project_dir(tmp_path, "github.com/o/r") == tmp_path / "projects" / "github.com" / "o" / "r"
    )
