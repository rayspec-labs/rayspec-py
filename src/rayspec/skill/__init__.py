# SPDX-License-Identifier: Apache-2.0
"""The packaged Claude Code skill (``rayspec/SKILL.md`` + ``references/``) and its installer.

Boundary: this package is *data* plus the small helpers that read and copy it. ``SKILL.md`` is
hand-written; ``references/*.md`` are generated from ``docs/*.md`` by ``scripts/gen_skill.py``
(the repository's ``.claude/skills/rayspec/`` is a mirror of this directory). The CLI
(:mod:`rayspec.cli.commands.skill`, :mod:`rayspec.cli.commands.init`) calls :func:`install_skill`
/ :func:`installed_state`; nothing here imports the loader, engine or providers. Files are read
through :mod:`importlib.resources`, so everything works from an installed wheel.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

#: Skill name — the directory name under ``.claude/skills/`` and the frontmatter ``name:``.
SKILL_NAME = "rayspec"

#: The ``docs/<name>.md`` pages mirrored into ``references/<name>.md`` (``scripts/gen_skill.py``).
REFERENCE_NAMES: tuple[str, ...] = (
    "concepts",
    "schema",
    "templating",
    "cli",
    "providers",
    "examples",
)

#: Where the skill is installed, relative to a project root or the home directory.
SKILLS_SUBDIR = Path(".claude") / "skills"

InstallAction = Literal["created", "overwritten", "skipped"]
InstalledStateKind = Literal["missing", "current", "stale"]


@dataclass(frozen=True, slots=True)
class InstalledFile:
    """One file of an install: its path relative to the skill dir and what was done with it."""

    relative: str
    path: Path
    action: InstallAction


@dataclass(frozen=True, slots=True)
class InstalledState:
    """What an install location holds: ``missing`` (no ``SKILL.md``), ``current`` (byte-identical
    to the packaged skill) or ``stale`` (differs — edited by hand or written by another rayspec
    version); ``digest`` is :func:`content_digest` over the files found there (``None`` when
    missing)."""

    path: Path
    state: InstalledStateKind
    digest: str | None


def skill_dir() -> Traversable:
    """The packaged skill directory (``…/rayspec/skill/rayspec``; holds ``SKILL.md``)."""
    return resources.files(__name__) / SKILL_NAME


def _walk(node: Traversable, prefix: str = "") -> list[tuple[str, Traversable]]:
    found: list[tuple[str, Traversable]] = []
    for child in node.iterdir():
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            if child.name != "__pycache__":
                found.extend(_walk(child, f"{rel}/"))
        elif not child.name.startswith(".") and not child.name.endswith((".py", ".pyc")):
            found.append((rel, child))
    return sorted(found, key=lambda item: item[0])


def skill_files() -> list[tuple[str, Traversable]]:
    """Every file of the packaged skill as ``[(relative posix path, resource)]``, sorted."""
    return _walk(skill_dir())


def _digest(items: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for rel, data in sorted(items):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()[:12]


def content_digest() -> str:
    """A short content hash of the packaged skill (``rayspec skill show`` prints it as the
    skill's version identity next to the rayspec version)."""
    return _digest([(rel, node.read_bytes()) for rel, node in skill_files()])


def _installed_files(root: Path) -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith(".") or "__pycache__" in path.parts:
            continue
        found.append((path.relative_to(root).as_posix(), path.read_bytes()))
    return found


def installed_state(target: Path) -> InstalledState:
    """Compare the skill directory ``target`` with the packaged skill."""
    if not (target / "SKILL.md").is_file():
        return InstalledState(target, "missing", None)
    digest = _digest(_installed_files(target))
    state: InstalledStateKind = "current" if digest == content_digest() else "stale"
    return InstalledState(target, state, digest)


def install_skill(target: Path, *, force: bool = False) -> list[InstalledFile]:
    """Write the packaged skill into ``target`` (the ``…/skills/rayspec`` directory itself).

    Existing files are kept (``skipped``) unless ``force`` (``overwritten``); missing ones are
    ``created``. Raises :class:`NotADirectoryError` when ``target`` (or a parent) is a file,
    :class:`IsADirectoryError` when a directory sits where a skill file goes, and any other
    :class:`OSError` unchanged — callers map them to ``error: …`` + exit 2.
    """
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(f"{target} is not a directory")
    target.mkdir(parents=True, exist_ok=True)
    results: list[InstalledFile] = []
    for rel, node in skill_files():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            raise IsADirectoryError(f"{path} is a directory, expected a file (or nothing)")
        existed = path.exists()
        if existed and not force:
            results.append(InstalledFile(rel, path, "skipped"))
            continue
        path.write_bytes(node.read_bytes())
        results.append(InstalledFile(rel, path, "overwritten" if existed else "created"))
    return results


def project_skill_dir(root: Path) -> Path:
    """``<root>/.claude/skills/rayspec`` — the project install location."""
    return root / SKILLS_SUBDIR / SKILL_NAME


def global_skill_dir(home: Path | None = None) -> Path:
    """``~/.claude/skills/rayspec`` — the user-wide install location (``home`` overrides ``~``)."""
    return (home or Path.home()) / SKILLS_SUBDIR / SKILL_NAME


__all__ = [
    "REFERENCE_NAMES",
    "SKILLS_SUBDIR",
    "SKILL_NAME",
    "InstalledFile",
    "InstalledState",
    "content_digest",
    "global_skill_dir",
    "install_skill",
    "installed_state",
    "project_skill_dir",
    "skill_dir",
    "skill_files",
]
