# SPDX-License-Identifier: Apache-2.0
"""The packaged Claude Code skills (``<name>/SKILL.md`` + ``references/``) and their installer.

rayspec ships **two** skills, because authoring a workflow and operating a run are two different
jobs with two different vocabularies: ``rayspec-workflows`` (the DSL — every step kind, field,
template rule) and ``rayspec-cli`` (every command, flag, ``--json`` shape and exit code). Each is
a :class:`Skill` in :data:`SKILLS`; nothing outside this module hard-codes a skill name.

Boundary: this package is *data* plus the small helpers that read and copy it. Each ``SKILL.md``
is hand-written; ``references/*.md`` are generated from ``docs/*.md`` by ``scripts/gen_skill.py``
(the repository's ``.claude/skills/<name>/`` are mirrors of these directories). The CLI
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

from rayspec.resources import walk_files

#: Where a skill is installed, relative to a project root or the home directory.
SKILLS_SUBDIR = Path(".claude") / "skills"

InstallAction = Literal["created", "overwritten", "skipped"]
InstalledStateKind = Literal["missing", "current", "stale"]


@dataclass(frozen=True, slots=True)
class Skill:
    """One packaged skill: the directory name (= the frontmatter ``name:`` and the directory it
    installs into under ``.claude/skills/``), a one-line summary for the CLI listings, and the
    ``docs/<name>.md`` pages mirrored into its ``references/``.

    A docs page belongs to exactly one skill: ``scripts/gen_skill.py`` keeps a link to a page of
    the *same* skill relative and rewrites every other target to the published docs URL, so a page
    two skills need is linked, never duplicated.
    """

    name: str
    summary: str
    references: tuple[str, ...]


#: Authoring: the workflow DSL, agents, prompts, stubs, project files.
WORKFLOWS_SKILL = Skill(
    name="rayspec-workflows",
    summary="authoring workflow YAML, agents, prompts and stubs (the DSL)",
    references=("concepts", "schema", "templating", "examples"),
)

#: Operating: running, inspecting, resuming, debugging, testing, auditing, governing.
CLI_SKILL = Skill(
    name="rayspec-cli",
    summary="running, inspecting, resuming, testing and governing rayspec (the CLI)",
    references=(
        "cli",
        "providers",
        "testing",
        "policy",
        "runs-and-resume",
        "isolation",
        "ci",
        "dogfooding",
    ),
)

#: Every packaged skill, in the order the CLI lists and installs them.
SKILLS: tuple[Skill, ...] = (WORKFLOWS_SKILL, CLI_SKILL)

#: The install names (``.claude/skills/<name>/``), in the same order.
SKILL_NAMES: tuple[str, ...] = tuple(skill.name for skill in SKILLS)


def find_skill(name: str) -> Skill | None:
    """The skill installed as ``name``, or ``None`` — callers turn that into a usage error."""
    return next((skill for skill in SKILLS if skill.name == name), None)


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


def skill_dir(skill: Skill) -> Traversable:
    """The packaged directory of ``skill`` (``…/rayspec/skill/<name>``; holds ``SKILL.md``)."""
    return resources.files(__name__) / skill.name


def skill_files(skill: Skill) -> list[tuple[str, Traversable]]:
    """Every file of ``skill`` as ``[(relative posix path, resource)]``, sorted.

    A skill is documentation: the Python that happens to live in the same package directory
    (and anything a build left behind) is not part of it.
    """
    return walk_files(
        skill_dir(skill),
        keep_dir=lambda _rel, name: name != "__pycache__",
        keep_file=lambda _rel, name: (
            not name.startswith(".") and not name.endswith((".py", ".pyc"))
        ),
    )


def _digest(items: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for rel, data in sorted(items):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()[:12]


def content_digest(skill: Skill) -> str:
    """A short content hash of one packaged skill (``rayspec skill show`` prints it as that
    skill's version identity next to the rayspec version)."""
    return _digest([(rel, node.read_bytes()) for rel, node in skill_files(skill)])


def _installed_files(root: Path) -> list[tuple[str, bytes]]:
    found: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith(".") or "__pycache__" in path.parts:
            continue
        found.append((path.relative_to(root).as_posix(), path.read_bytes()))
    return found


def installed_state(skill: Skill, target: Path) -> InstalledState:
    """Compare the directory ``target`` with the packaged ``skill``."""
    if not (target / "SKILL.md").is_file():
        return InstalledState(target, "missing", None)
    digest = _digest(_installed_files(target))
    state: InstalledStateKind = "current" if digest == content_digest(skill) else "stale"
    return InstalledState(target, state, digest)


def install_skill(skill: Skill, target: Path, *, force: bool = False) -> list[InstalledFile]:
    """Write ``skill`` into ``target`` (the ``…/skills/<name>`` directory itself).

    Existing files are kept (``skipped``) unless ``force`` (``overwritten``); missing ones are
    ``created``. Raises :class:`NotADirectoryError` when ``target`` (or a parent) is a file,
    :class:`IsADirectoryError` when a directory sits where a skill file goes, and any other
    :class:`OSError` unchanged — callers map them to ``error: …`` + exit 2.
    """
    if target.exists() and not target.is_dir():
        raise NotADirectoryError(f"{target} is not a directory")
    target.mkdir(parents=True, exist_ok=True)
    results: list[InstalledFile] = []
    for rel, node in skill_files(skill):
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


def project_skill_dir(skill: Skill, root: Path) -> Path:
    """``<root>/.claude/skills/<name>`` — the project install location of ``skill``."""
    return root / SKILLS_SUBDIR / skill.name


def global_skill_dir(skill: Skill, home: Path | None = None) -> Path:
    """``~/.claude/skills/<name>`` — the user-wide location (``home`` overrides ``~``)."""
    return (home or Path.home()) / SKILLS_SUBDIR / skill.name


__all__ = [
    "CLI_SKILL",
    "SKILLS",
    "SKILLS_SUBDIR",
    "SKILL_NAMES",
    "WORKFLOWS_SKILL",
    "InstalledFile",
    "InstalledState",
    "Skill",
    "content_digest",
    "find_skill",
    "global_skill_dir",
    "install_skill",
    "installed_state",
    "project_skill_dir",
    "skill_dir",
    "skill_files",
]
