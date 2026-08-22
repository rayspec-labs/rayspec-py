"""Shared helpers for the policy tests: a temp project + user home with policy files.

The suite writes real files because the whole point of the policy layer is *which file on disk*
imposed a restriction: a fake in-memory layer would not exercise the line numbers that end up in
the error messages.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class Tree:
    """A temp project (``root/.rayspec``) plus a user home, with write helpers."""

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

    def policy(self, text: str, *, user: bool = False) -> Path:
        return self.write("policy.yaml", text, user=user)


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


def validated(tree: Tree, text: str, *, name: str = "wf", **kwargs):
    """Load ``text`` as a workflow of ``tree`` and validate it against the real capabilities."""
    from rayspec.loader import load_workflow, validate_workflow
    from rayspec.providers.capabilities import BUILTIN_CAPABILITIES

    tree.workflow(name, text)
    rw = load_workflow(name, project_root=tree.root, home=tree.home)
    kwargs.setdefault("capabilities_for", BUILTIN_CAPABILITIES.get)
    kwargs.setdefault("provider_ids", sorted(BUILTIN_CAPABILITIES))
    return rw, validate_workflow(rw, **kwargs)
