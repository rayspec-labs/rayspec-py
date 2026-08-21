"""pytest fixtures for the workspace tests (helpers live in :mod:`tests.workspace.gitfixtures`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from .gitfixtures import GIT_ENV, git, make_repo


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with two commits on ``main`` and an ``origin`` remote URL."""
    r = make_repo(tmp_path / "repo", commits=2)
    git("remote", "add", "origin", "git@github.com:Acme/Widget.git", cwd=r)
    return r
