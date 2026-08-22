#!/usr/bin/env python3
"""Generate the reference files of the packaged skills and mirror their directories.

rayspec ships two skills under ``src/rayspec/skill/<name>/`` (:data:`rayspec.skill.SKILLS`):
``rayspec-workflows`` (authoring the DSL) and ``rayspec-cli`` (operating the CLI). Each is a
hand-written ``SKILL.md`` plus ``references/<name>.md`` — near-verbatim copies of ``docs/<name>.md``
for every name in that skill's :attr:`~rayspec.skill.Skill.references`, each with a three-line
header, relative links rewritten (to the sibling reference file *of the same skill*, or to the
published docs URL of ``rayspec.cli._docs.DOCS_BASE`` for every other target) and this
repository's docs-as-tests markers stripped (:func:`strip_markers`). Each skill dir is then
mirrored to ``.claude/skills/<name>/`` so this repository's own coding-agent sessions use them
too. Run it after editing ``docs/*.md`` or a ``SKILL.md``::

    uv run python scripts/gen_skill.py          # rewrite references + mirror
    uv run python scripts/gen_skill.py --check  # exit 1 when anything is stale

``tests/skill/test_skill_fresh.py`` runs the check in the test suite.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import sys
from pathlib import Path

from rayspec.cli._docs import DOCS_BASE
from rayspec.skill import SKILLS, Skill

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
SKILL_ROOT = REPO_ROOT / "src" / "rayspec" / "skill"
MIRROR_ROOT = REPO_ROOT / ".claude" / "skills"

#: Files of a skill dir that are not generated (kept as they are, mirrored verbatim).
HAND_WRITTEN = ("SKILL.md",)

_LINK_RE = re.compile(r"(?<!\\)(\[[^\]]*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")
#: The docs-as-tests markers of ``docs/*.md`` (``scripts/check_examples.py``). They name a check
#: that exists only in this repository, so they are dropped on the way into the references.
_MARKER_RE = re.compile(r"^[ \t]*<!--\s*rayspec:(?:validate|run|skip)\b.*-->[ \t]*$")


def skill_src(skill: Skill) -> Path:
    """The packaged directory of ``skill`` in this checkout."""
    return SKILL_ROOT / skill.name


def references_dir(skill: Skill) -> Path:
    """``src/rayspec/skill/<name>/references``."""
    return skill_src(skill) / "references"


def mirror_dir(skill: Skill) -> Path:
    """``.claude/skills/<name>`` — this repository's own copy of ``skill``."""
    return MIRROR_ROOT / skill.name


def strip_markers(text: str) -> str:
    """``docs/<name>.md`` without its docs-as-tests marker comments.

    The references are model-facing input for the packaged skills: an agent reading them would see
    a convention it has no way to satisfy, and could imitate it when writing docs or workflows for
    somebody else's project. The rendered page never showed them either — they are HTML comments.
    """
    kept = [line for line in text.splitlines(keepends=True) if not _MARKER_RE.match(line.rstrip())]
    return "".join(kept)


def header_for(skill: Skill, name: str) -> str:
    """The three-line header every generated reference starts with."""
    siblings = " · ".join(f"{n}.md" for n in skill.references)
    return (
        f"<!-- Generated from docs/{name}.md by scripts/gen_skill.py — do not edit here. -->\n"
        f"<!-- Canonical source: {DOCS_BASE}docs/{name}.md -->\n"
        f"<!-- Sibling references in this directory: {siblings} -->\n"
        "\n"
    )


def rewrite_link(skill: Skill, target: str) -> str:
    """Rewrite one relative link target of ``docs/<name>.md`` for ``skill``'s references dir.

    Absolute URLs, ``mailto:`` and ``#anchor``-only targets are returned unchanged; a link to
    another page of the *same* skill stays a sibling link (``schema.md#inputs``); anything else
    (a page of the other skill, ``../examples/…``, ``../schemas``) becomes the published docs URL.
    That is why a page two skills need is never duplicated: the second one links to it.
    """
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return target
    file_part, sep, anchor = target.partition("#")
    stem = Path(file_part).stem
    if Path(file_part).parent == Path() and file_part.endswith(".md") and stem in skill.references:
        return target
    rel = posixpath.normpath(posixpath.join("docs", file_part))
    return f"{DOCS_BASE}{rel}{sep}{anchor}"


def render_reference(skill: Skill, name: str) -> str:
    """The generated ``references/<name>.md``: header + the page, links rewritten, markers gone."""
    text = strip_markers((DOCS_DIR / f"{name}.md").read_text(encoding="utf-8"))
    body = _LINK_RE.sub(
        lambda m: f"{m.group(1)}{rewrite_link(skill, m.group(2))}{m.group(3)}", text
    )
    return header_for(skill, name) + body


def expected_references(skill: Skill) -> dict[Path, str]:
    """``{references/<name>.md: content}`` for every reference name of ``skill``."""
    return {references_dir(skill) / f"{n}.md": render_reference(skill, n) for n in skill.references}


def skill_source_files(skill: Skill) -> dict[Path, bytes]:
    """Every file of one packaged skill dir (relative path → bytes), generated ones included."""
    found: dict[Path, bytes] = {}
    for path in sorted(skill_src(skill).rglob("*")):
        if path.is_file() and not path.name.startswith(".") and "__pycache__" not in path.parts:
            found[path.relative_to(skill_src(skill))] = path.read_bytes()
    return found


def stale_items() -> list[str]:
    """Human-readable list of everything that differs from the generated state (empty = fresh)."""
    problems: list[str] = []
    for skill in SKILLS:
        problems.extend(_stale_for(skill))
    for extra in sorted(MIRROR_ROOT.iterdir()) if MIRROR_ROOT.is_dir() else []:
        if extra.is_dir() and extra.name not in {s.name for s in SKILLS}:
            problems.append(f"{extra.relative_to(REPO_ROOT)}: not a packaged skill (remove it)")
    return problems


def _stale_for(skill: Skill) -> list[str]:
    problems: list[str] = []
    for path, content in expected_references(skill).items():
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file():
            problems.append(f"{rel}: missing")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"{rel}: stale")
    expected_names = {f"{n}.md" for n in skill.references}
    refs = references_dir(skill)
    if refs.is_dir():
        for extra in sorted(refs.iterdir()):
            if extra.is_file() and extra.name not in expected_names:
                problems.append(f"{extra.relative_to(REPO_ROOT)}: unexpected file")
    source = skill_source_files(skill)
    mirror: dict[Path, bytes] = {}
    target = mirror_dir(skill)
    if target.is_dir():
        for path in sorted(target.rglob("*")):
            if path.is_file():
                mirror[path.relative_to(target)] = path.read_bytes()
    for rel in sorted(set(source) | set(mirror)):
        shown = target.relative_to(REPO_ROOT) / rel
        if rel not in mirror:
            problems.append(f"{shown}: missing in mirror")
        elif rel not in source:
            problems.append(f"{shown}: not in the packaged skill")
        elif mirror[rel] != source[rel]:
            problems.append(f"{shown}: differs from the packaged skill")
    return problems


def generate() -> list[str]:
    """Write the references and the mirrors; returns the paths that changed."""
    changed: list[str] = []
    for skill in SKILLS:
        changed.extend(_generate_for(skill))
    if MIRROR_ROOT.is_dir():
        for extra in sorted(MIRROR_ROOT.iterdir()):
            if extra.is_dir() and extra.name not in {s.name for s in SKILLS}:
                shutil.rmtree(extra)
                changed.append(f"{extra.relative_to(REPO_ROOT)} (removed)")
    return changed


def _generate_for(skill: Skill) -> list[str]:
    changed: list[str] = []
    refs = references_dir(skill)
    refs.mkdir(parents=True, exist_ok=True)
    expected = expected_references(skill)
    for path, content in expected.items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            changed.append(str(path.relative_to(REPO_ROOT)))
    for extra in sorted(refs.iterdir()):
        if extra.is_file() and extra not in expected:
            extra.unlink()
            changed.append(f"{extra.relative_to(REPO_ROOT)} (removed)")
    source = skill_source_files(skill)
    target = mirror_dir(skill)
    if target.is_dir():
        for path in sorted(target.rglob("*")):
            if path.is_file() and path.relative_to(target) not in source:
                path.unlink()
                changed.append(f"{path.relative_to(REPO_ROOT)} (removed)")
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    for rel, data in source.items():
        dest = target / rel
        if dest.is_file() and dest.read_bytes() == data:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(skill_src(skill) / rel, dest)
        changed.append(str(dest.relative_to(REPO_ROOT)))
    return changed


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("--check", action="store_true", help="do not write; exit 1 when stale")
    args = parser.parse_args(argv)
    if args.check:
        problems = stale_items()
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            print("skills are stale; run `uv run python scripts/gen_skill.py`", file=sys.stderr)
            return 1
        print("skills: up to date")
        return 0
    changed = generate()
    if not changed:
        print("skills: up to date")
    for item in changed:
        print(f"wrote {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
