"""Relative links in ``docs/*.md`` and ``README.md`` must resolve (files and ``#anchors``)."""

from __future__ import annotations

import re
from pathlib import Path

# [text](target) — ignores images' alt text handling (images are links too) and reference-style
_LINK_RE = re.compile(r"(?<!\\)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code(text: str) -> str:
    """Remove fenced blocks and inline code so links inside examples are not checked."""
    text = _FENCE_RE.sub("", text)
    return _INLINE_CODE_RE.sub("", text)


def slugify(heading: str) -> str:
    """GitHub-style anchor: lowercase, drop punctuation (keep ``-``/``_``), spaces → ``-``."""
    text = _INLINE_CODE_RE.sub(lambda m: m.group(0)[1:-1], heading)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    text = _FENCE_RE.sub("", path.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    out: set[str] = set()
    for match in _HEADING_RE.finditer(text):
        slug = slugify(match.group(2))
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


def links_of(path: Path) -> list[str]:
    return _LINK_RE.findall(_strip_code(path.read_text(encoding="utf-8")))


def test_there_are_docs_to_check(markdown_files: list[Path]) -> None:
    names = {p.name for p in markdown_files}
    assert "README.md" in names
    assert len(markdown_files) > 5


def test_relative_links_resolve(markdown_files: list[Path]) -> None:
    problems: list[str] = []
    for md in markdown_files:
        for target in links_of(md):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _, anchor = target.partition("#")
            dest = md if not file_part else (md.parent / file_part).resolve()
            if not dest.exists():
                problems.append(f"{md.name}: broken link {target!r} (missing {dest})")
                continue
            if anchor and dest.suffix == ".md" and anchor not in anchors_of(dest):
                problems.append(f"{md.name}: no heading for anchor {target!r} in {dest.name}")
    assert not problems, "\n".join(problems)


def test_docs_link_each_other_from_the_readme(markdown_files: list[Path], repo_root: Path) -> None:
    """Every docs page is reachable from README.md (the entry point)."""
    readme = repo_root / "README.md"
    linked = {
        (readme.parent / t.partition("#")[0]).resolve()
        for t in links_of(readme)
        if not t.startswith(("http", "mailto:", "#"))
    }
    missing = [p.name for p in markdown_files if p != readme and p.resolve() not in linked]
    assert not missing, f"README.md does not link: {missing}"
