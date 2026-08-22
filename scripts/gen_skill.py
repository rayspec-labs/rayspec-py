#!/usr/bin/env python3
"""Generate the reference files of the packaged ``rayspec`` skill and mirror the skill dir.

The skill ships with the package under ``src/rayspec/skill/rayspec/``: a hand-written
``SKILL.md`` plus ``references/<name>.md`` — verbatim copies of ``docs/<name>.md`` for every
name in :data:`rayspec.skill.REFERENCE_NAMES`, each with a three-line header and relative links
rewritten (to the sibling reference file, or to the published docs URL of
``rayspec.cli._docs.DOCS_BASE`` when the target is not part of the skill). The whole skill dir is
then mirrored to ``.claude/skills/rayspec/`` so this repository's own coding-agent sessions use
it too. Run it after editing ``docs/*.md`` or ``SKILL.md``::

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
from rayspec.skill import REFERENCE_NAMES, SKILL_NAME

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
SKILL_SRC = REPO_ROOT / "src" / "rayspec" / "skill" / SKILL_NAME
REFERENCES_DIR = SKILL_SRC / "references"
MIRROR_DIR = REPO_ROOT / ".claude" / "skills" / SKILL_NAME

#: Files of the skill dir that are not generated (kept as they are, mirrored verbatim).
HAND_WRITTEN = ("SKILL.md",)

_LINK_RE = re.compile(r"(?<!\\)(\[[^\]]*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")


def header_for(name: str) -> str:
    """The three-line header every generated reference starts with."""
    siblings = " · ".join(f"{n}.md" for n in REFERENCE_NAMES)
    return (
        f"<!-- Generated from docs/{name}.md by scripts/gen_skill.py — do not edit here. -->\n"
        f"<!-- Canonical source: {DOCS_BASE}docs/{name}.md -->\n"
        f"<!-- Sibling references in this directory: {siblings} -->\n"
        "\n"
    )


def rewrite_link(target: str) -> str:
    """Rewrite one relative link target of ``docs/<name>.md`` for the references directory.

    Absolute URLs, ``mailto:`` and ``#anchor``-only targets are returned unchanged; a link to
    another generated page stays a sibling link (``schema.md#inputs``); anything else
    (``runs-and-resume.md``, ``../examples/…``, ``../schemas``) becomes the published docs
    URL.
    """
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return target
    file_part, sep, anchor = target.partition("#")
    stem = Path(file_part).stem
    if Path(file_part).parent == Path() and file_part.endswith(".md") and stem in REFERENCE_NAMES:
        return target
    rel = posixpath.normpath(posixpath.join("docs", file_part))
    return f"{DOCS_BASE}{rel}{sep}{anchor}"


def render_reference(name: str) -> str:
    """The generated ``references/<name>.md``: header + ``docs/<name>.md`` with links rewritten."""
    text = (DOCS_DIR / f"{name}.md").read_text(encoding="utf-8")
    body = _LINK_RE.sub(lambda m: f"{m.group(1)}{rewrite_link(m.group(2))}{m.group(3)}", text)
    return header_for(name) + body


def expected_references() -> dict[Path, str]:
    """``{references/<name>.md: content}`` for every reference name."""
    return {REFERENCES_DIR / f"{name}.md": render_reference(name) for name in REFERENCE_NAMES}


def skill_source_files() -> dict[Path, bytes]:
    """Every file of the packaged skill dir (relative path → bytes), generated ones included."""
    found: dict[Path, bytes] = {}
    for path in sorted(SKILL_SRC.rglob("*")):
        if path.is_file() and not path.name.startswith(".") and "__pycache__" not in path.parts:
            found[path.relative_to(SKILL_SRC)] = path.read_bytes()
    return found


def stale_items() -> list[str]:
    """Human-readable list of everything that differs from the generated state (empty = fresh)."""
    problems: list[str] = []
    for path, content in expected_references().items():
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file():
            problems.append(f"{rel}: missing")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"{rel}: stale")
    expected_names = {f"{n}.md" for n in REFERENCE_NAMES}
    if REFERENCES_DIR.is_dir():
        for extra in sorted(REFERENCES_DIR.iterdir()):
            if extra.is_file() and extra.name not in expected_names:
                problems.append(f"{extra.relative_to(REPO_ROOT)}: unexpected file")
    source = skill_source_files()
    mirror: dict[Path, bytes] = {}
    if MIRROR_DIR.is_dir():
        for path in sorted(MIRROR_DIR.rglob("*")):
            if path.is_file():
                mirror[path.relative_to(MIRROR_DIR)] = path.read_bytes()
    for rel in sorted(set(source) | set(mirror)):
        shown = MIRROR_DIR.relative_to(REPO_ROOT) / rel
        if rel not in mirror:
            problems.append(f"{shown}: missing in mirror")
        elif rel not in source:
            problems.append(f"{shown}: not in the packaged skill")
        elif mirror[rel] != source[rel]:
            problems.append(f"{shown}: differs from the packaged skill")
    return problems


def generate() -> list[str]:
    """Write the references and the mirror; returns the paths that changed."""
    changed: list[str] = []
    REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    expected = expected_references()
    for path, content in expected.items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            changed.append(str(path.relative_to(REPO_ROOT)))
    for extra in sorted(REFERENCES_DIR.iterdir()):
        if extra.is_file() and extra not in expected:
            extra.unlink()
            changed.append(f"{extra.relative_to(REPO_ROOT)} (removed)")
    source = skill_source_files()
    if MIRROR_DIR.is_dir():
        for path in sorted(MIRROR_DIR.rglob("*")):
            if path.is_file() and path.relative_to(MIRROR_DIR) not in source:
                path.unlink()
                changed.append(f"{path.relative_to(REPO_ROOT)} (removed)")
        for path in sorted(MIRROR_DIR.rglob("*"), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    for rel, data in source.items():
        target = MIRROR_DIR / rel
        if target.is_file() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SKILL_SRC / rel, target)
        changed.append(str(target.relative_to(REPO_ROOT)))
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
            print("skill is stale; run `uv run python scripts/gen_skill.py`", file=sys.stderr)
            return 1
        print("skill: up to date")
        return 0
    changed = generate()
    if not changed:
        print("skill: up to date")
    for item in changed:
        print(f"wrote {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
