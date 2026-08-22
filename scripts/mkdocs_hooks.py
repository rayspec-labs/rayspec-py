#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""MkDocs hooks: publish the repository-root pages and fix the links that leave ``docs/``.

Boundary: build-time only — nothing here is imported by ``rayspec``. The docs are written to be
read **on GitHub first**: ``docs/schema.md`` links ``../examples/secret_via_tool/`` and the
project README links ``docs/cli.md``. Those targets do not exist inside the built site, so this
hook

* adds ``index.md`` (the project README) and ``changelog.md`` (CHANGELOG.md) as generated pages,
  which is why neither file is duplicated into ``docs/``; and
* rewrites every relative link of every page: to another published page when the target is one,
  to a full ``blob/main`` / ``tree/main`` URL when it is not.

Fenced code blocks are left alone — a link inside a sample is part of the sample.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Generated page → the repository-root file it is generated from.
ROOT_PAGES = {"index.md": "README.md", "changelog.md": "CHANGELOG.md"}

#: ``[text](target "title")``, including a badge whose text is itself an image link.
_LINK_RE = re.compile(
    r"(?<!\\)(\[(?:[^\[\]]|!\[[^\]]*\]\([^)]*\))*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))"
)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:", "#", "/"))


def repo_link(repo_url: str, path: Path) -> str:
    """The GitHub URL of a checkout path (``tree/main`` for a directory, ``blob/main`` else)."""
    rel = path.relative_to(REPO_ROOT).as_posix()
    kind = "tree" if path.is_dir() else "blob"
    return f"{repo_url.rstrip('/')}/{kind}/main/{rel}"


def rewrite_target(target: str, *, source_dir: Path, docs_dir: Path, repo_url: str) -> str:
    """One link target of a page whose relative links resolve against *source_dir*.

    A target that is a published page of the site keeps its relative form (mkdocs turns
    ``schema.md#inputs`` into the right URL); everything else becomes a repository URL, so the
    reader lands on the file instead of a 404.
    """
    if _is_external(target):
        return target
    file_part, sep, anchor = target.partition("#")
    if not file_part:
        return target
    dest = Path(source_dir, file_part)
    try:
        resolved = dest.resolve()
    except OSError:  # pragma: no cover - only a malformed path gets here
        return target
    if not resolved.exists():
        return target
    for page, source in ROOT_PAGES.items():
        if resolved == (REPO_ROOT / source).resolve():
            return page + sep + anchor
    if resolved.parent == docs_dir.resolve() and resolved.suffix == ".md":
        if resolved.name == "README.md":  # the GitHub index; the site nav replaces it
            return repo_link(repo_url, resolved)
        return resolved.name + sep + anchor
    return repo_link(repo_url, resolved)


def rewrite_links(markdown: str, *, source_dir: Path, docs_dir: Path, repo_url: str) -> str:
    """Rewrite every link of *markdown* outside fenced code blocks."""
    out: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines(keepends=True):
        marker = _FENCE_RE.match(line)
        if fence is None and marker:
            fence = marker.group(1)
        elif fence is not None and marker and marker.group(1) == fence:
            fence = None
        if fence is not None:
            out.append(line)
            continue
        out.append(
            _LINK_RE.sub(
                lambda m: (
                    m.group(1)
                    + rewrite_target(
                        m.group(2), source_dir=source_dir, docs_dir=docs_dir, repo_url=repo_url
                    )
                    + m.group(3)
                ),
                line,
            )
        )
    return "".join(out)


def on_files(files: Any, config: Any) -> Any:
    """Add the generated root pages (``index.md``, ``changelog.md``) to the build."""
    from mkdocs.structure.files import File

    for src_uri, source in ROOT_PAGES.items():
        content = (REPO_ROOT / source).read_text(encoding="utf-8")
        files.append(File.generated(config, src_uri, content=content))
    return files


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    """Repair the relative links of one page before it is rendered."""
    docs_dir = Path(config["docs_dir"])
    source_dir = REPO_ROOT if page.file.src_uri in ROOT_PAGES else docs_dir
    return rewrite_links(
        markdown,
        source_dir=source_dir,
        docs_dir=docs_dir,
        repo_url=config["repo_url"],
    )
