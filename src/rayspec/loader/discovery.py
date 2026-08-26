# SPDX-License-Identifier: Apache-2.0
"""Discovery of workflow and agent files: project ``.rayspec/`` first, then ``~/.rayspec/``, and —
for workflows only — the library bundled with rayspec last."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from rayspec.config.paths import rayspec_home
from rayspec.errors import RayspecError
from rayspec.loader.bundled import BUNDLED_SCOPE, bundled_dir
from rayspec.loader.yaml import load_yaml

Scope = Literal["project", "user", "bundled"]
YAML_SUFFIXES: tuple[str, ...] = (".yaml", ".yml")


@dataclass(frozen=True, slots=True)
class WorkflowRef:
    """A discovered workflow file. ``name`` is the file stem (what the CLI accepts).

    Discovery is a **directory listing**: a name, a path and a scope. :attr:`description`,
    :attr:`error` and :attr:`inputs` need the file's *contents*, so they are read on first access
    and remembered — a workflow nobody asks about is never opened.

    Why lazily and not up front: every command that resolves a workflow by name discovers the
    whole project to do it (:func:`rayspec.cli.commands._loader_common.workflow_label`), so
    ``rayspec validate`` discovers it once per name it was given. With the description parsed
    eagerly that made validating a project of N workflows cost N² full YAML parses — 32 s at
    N=200, and rising with the square. Only ``rayspec workflows`` and ``rayspec doctor`` ever
    read these two fields.
    """

    name: str
    path: Path
    scope: Scope
    #: The bundled file this project/user file shadows (same stem), or ``None`` — a bundled ref
    #: never shadows anything. What ``rayspec workflows`` reports as ``overridden``.
    overrides: Path | None = None
    #: Memo for :attr:`description`/:attr:`error`/:attr:`inputs` — a ref reads its file at most
    #: once. Excluded from equality and repr: it is a cache, not part of what a discovered
    #: workflow IS.
    _described: list[tuple[str, str | None, dict[str, Any]]] = field(
        default_factory=list, compare=False, repr=False
    )

    @property
    def description(self) -> str:
        """The workflow's ``description:``; ``""`` when it has none or the file cannot be read."""
        return self._describe()[0]

    @property
    def error(self) -> str | None:
        """Why this file could not be read as a workflow mapping, or ``None`` when it can."""
        return self._describe()[1]

    @property
    def inputs(self) -> dict[str, Any]:
        """The raw ``inputs:`` mapping of the file — unvalidated; ``{}`` when absent/unreadable."""
        return self._describe()[2]

    def _describe(self) -> tuple[str, str | None, dict[str, Any]]:
        if not self._described:
            self._described.append(_describe(self.path))
        return self._described[0]


@dataclass(frozen=True, slots=True)
class AgentFileRef:
    """A discovered ``agents/<name>.yaml`` file (one :class:`AgentDef` per file)."""

    name: str
    path: Path
    scope: Scope


def project_rayspec_dir(project_root: Path) -> Path:
    """``<project_root>/.rayspec``."""
    return project_root / ".rayspec"


def find_project_root(start: Path | None = None) -> Path:
    """The project root for ``start`` (default: the process cwd), resolved.

    Walks up from ``start`` to the nearest directory containing a ``.rayspec/`` directory. If
    there is none, the nearest enclosing git repository (a directory holding ``.git``, file or
    directory, so worktrees and submodules count) is the root.

    **When there is no project at all** — no ``.rayspec/`` and no git repository above ``start``
    — the answer is ``start`` itself, resolved. Nothing is created and nothing is guessed: the
    caller gets a directory it can read a config from and list an (empty) set of workflows in.
    A command that must not treat an arbitrary directory as a project — ``rayspec runs`` and
    ``rayspec costs``, which would otherwise mint a project slug and a store for one — re-checks
    for ``.rayspec/`` or ``.git`` itself and says so instead.

    This is the one project-root discovery. The git top level of a directory is a different
    question with a different answer (``packages/foo/.rayspec`` in a monorepo) and is answered
    by :func:`rayspec.workspace.git.toplevel`.
    """
    here = (Path.cwd() if start is None else start).resolve()
    git_root: Path | None = None
    for candidate in (here, *here.parents):
        if (candidate / ".rayspec").is_dir():
            return candidate
        if git_root is None and (candidate / ".git").exists():
            git_root = candidate
    return git_root or here


def _yaml_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix in YAML_SUFFIXES),
        key=lambda p: p.name,
    )


def _describe(path: Path) -> tuple[str, str | None, dict[str, Any]]:
    try:
        data = load_yaml(path.read_text(encoding="utf-8"), source=str(path))
    except (RayspecError, OSError) as exc:
        return "", str(exc), {}
    if not isinstance(data, dict):
        return "", f"{path}: workflow must be a mapping", {}
    description = data.get("description")
    inputs = data.get("inputs")
    return (
        (description if isinstance(description, str) else ""),
        None,
        (dict(inputs) if isinstance(inputs, dict) else {}),
    )


def _scoped_dirs(project_root: Path, home: Path | None, sub: str) -> list[tuple[Scope, Path]]:
    home = rayspec_home() if home is None else home
    return [("project", project_rayspec_dir(project_root) / sub), ("user", home / sub)]


def discover_workflows(project_root: Path, *, home: Path | None = None) -> list[WorkflowRef]:
    """List workflows from ``.rayspec/workflows/``, ``<home>/workflows/`` and the bundled library.

    Precedence on a name clash is project → user → bundled: the first scope to claim a stem
    keeps it. A project or user file whose stem a bundled workflow also has *shadows* that file
    and records it in :attr:`WorkflowRef.overrides`. On an installed package the list is never
    empty — the library is what a fresh checkout can run before it has a ``.rayspec/``.
    """
    found: dict[str, WorkflowRef] = {}
    for scope, directory in _scoped_dirs(project_root, home, "workflows"):
        for path in _yaml_files(directory):
            if path.stem not in found:
                found[path.stem] = WorkflowRef(name=path.stem, path=path, scope=scope)
    for path in _yaml_files(bundled_dir()):
        existing = found.get(path.stem)
        found[path.stem] = (
            WorkflowRef(name=path.stem, path=path, scope=BUNDLED_SCOPE)
            if existing is None
            else replace(existing, overrides=path)
        )
    return [found[name] for name in sorted(found)]


def discover_agents(project_root: Path, *, home: Path | None = None) -> list[AgentFileRef]:
    """List agent files from ``.rayspec/agents/`` and ``<home>/agents/`` (project wins)."""
    found: dict[str, AgentFileRef] = {}
    for scope, directory in _scoped_dirs(project_root, home, "agents"):
        for path in _yaml_files(directory):
            if path.stem not in found:
                found[path.stem] = AgentFileRef(name=path.stem, path=path, scope=scope)
    return [found[name] for name in sorted(found)]


__all__ = [
    "YAML_SUFFIXES",
    "AgentFileRef",
    "Scope",
    "WorkflowRef",
    "discover_agents",
    "discover_workflows",
    "find_project_root",
    "project_rayspec_dir",
]
