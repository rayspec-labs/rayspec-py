"""Shared helpers for loader tests: build temp .rayspec trees."""

from __future__ import annotations

from pathlib import Path

import pytest


class Tree:
    """A temp project (``root/.rayspec``) + user home (``home``) with write helpers."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "proj"
        self.home = tmp_path / "home"
        self.rayspec = self.root / ".rayspec"
        for sub in ("workflows", "agents", "prompts"):
            (self.rayspec / sub).mkdir(parents=True, exist_ok=True)
            (self.home / sub).mkdir(parents=True, exist_ok=True)

    def write(self, rel: str, text: str, *, user: bool = False) -> Path:
        base = self.home if user else self.rayspec
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def workflow(self, name: str, text: str, *, user: bool = False) -> Path:
        return self.write(f"workflows/{name}.yaml", text, user=user)

    def agent(self, name: str, text: str, *, user: bool = False) -> Path:
        return self.write(f"agents/{name}.yaml", text, user=user)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)
