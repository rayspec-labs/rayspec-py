"""Shared helpers: build real temporary git repositories (no network)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

GIT_ENV = {
    "GIT_AUTHOR_NAME": "rayspec-test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "rayspec-test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,  # ignore the user's ~/.gitconfig (hooks, signing, ...)
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(*args: str, cwd: Path) -> str:
    """Run git with a deterministic identity; return stripped stdout (raises on failure)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    return proc.stdout.strip()


def make_repo(path: Path, *, branch: str = "main", commits: int = 1) -> Path:
    """``git init`` + ``commits`` commits on ``branch`` (each touching ``file<n>.txt``)."""
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", branch, cwd=path)
    for n in range(commits):
        (path / f"file{n}.txt").write_text(f"content {n}\n")
        git("add", ".", cwd=path)
        git("commit", "-q", "-m", f"commit {n}", cwd=path)
    return path
