"""The published docs site is built from ``docs/`` and every internal link resolves in it.

Boundary: ``mkdocs.yml`` plus the built HTML — no network (the build reads only the checkout).
The docs are written to be read on GitHub *and* on the site, so a link may not be correct in one
rendering and broken in the other; that is what the built-site link walk below pins. The build
needs the ``docs`` dependency group (``uv sync --all-groups``); without it these tests skip
rather than pretend.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"
DOCS_DIR = REPO_ROOT / "docs"
#: Pages the site generates from repository-root files rather than from ``docs/``.
GENERATED = {"index.md", "changelog.md"}


class _Loader(yaml.SafeLoader):
    """``mkdocs.yml`` may carry mkdocs' own YAML tags; they are irrelevant to these assertions."""


def _ignore_tag(loader: yaml.Loader, suffix: str, node: yaml.Node) -> None:
    return None


_Loader.add_multi_constructor("tag:yaml.org,2002:python/", _ignore_tag)
_Loader.add_multi_constructor("!", _ignore_tag)


def config() -> dict[str, Any]:
    """``mkdocs.yml`` as plain data."""
    return yaml.load(MKDOCS_YML.read_text(encoding="utf-8"), Loader=_Loader)


def nav_targets(nav: Any) -> list[str]:
    """Every page file named anywhere in the ``nav:`` tree, in order."""
    out: list[str] = []
    if isinstance(nav, str):
        out.append(nav)
    elif isinstance(nav, list):
        for entry in nav:
            out.extend(nav_targets(entry))
    elif isinstance(nav, dict):
        for value in nav.values():
            out.extend(nav_targets(value))
    return out


class _Links(HTMLParser):
    """Collect every ``href``/``src`` of a built page."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"href", "src"} and value:
                self.found.append(value)


def links_of(html: str) -> list[str]:
    parser = _Links()
    parser.feed(html)
    return parser.found


def anchors_of(html: str) -> set[str]:
    """Every fragment target a page offers (``id=`` and ``name=``)."""

    class _Ids(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.found: set[str] = set()

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            for name, value in attrs:
                if name in {"id", "name"} and value:
                    self.found.add(value)

    parser = _Ids()
    parser.feed(html)
    return parser.found


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The site built exactly as CI builds it: ``mkdocs build --strict``."""
    pytest.importorskip("mkdocs", reason="the docs group is not installed (uv sync --all-groups)")
    out = tmp_path_factory.mktemp("site")
    done = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"mkdocs build --strict failed:\n{done.stdout}\n{done.stderr}"
    return out


def test_the_site_config_is_there_and_points_at_the_docs_directory() -> None:
    cfg = config()
    assert cfg["site_name"]
    assert cfg["docs_dir"] == "docs"
    assert cfg["repo_url"].endswith("rayspec-labs/rayspec-py")


def test_the_nav_lists_every_page_in_the_docs_directory() -> None:
    """A page nobody navigates to is a page nobody reads — adding one has to force a nav edit."""
    listed = {Path(target).name for target in nav_targets(config().get("nav"))}
    on_disk = {path.name for path in DOCS_DIR.glob("*.md")} - {"README.md"}
    assert on_disk - listed == set(), f"not in the site nav: {sorted(on_disk - listed)}"


def test_every_nav_entry_is_a_page_that_exists_or_is_generated() -> None:
    missing = [
        target
        for target in nav_targets(config().get("nav"))
        if target not in GENERATED and not (DOCS_DIR / target).is_file()
    ]
    assert not missing, f"the nav points at pages that do not exist: {missing}"


def test_the_docs_index_and_the_site_nav_do_not_drift() -> None:
    """``docs/README.md`` is the index on GitHub; the nav is the index on the site."""
    index = (DOCS_DIR / "README.md").read_text(encoding="utf-8")
    listed = {Path(target).name for target in nav_targets(config().get("nav"))}
    linked = {
        line.split("](", 1)[1].split(")", 1)[0]
        for line in index.splitlines()
        if "](" in line and line.startswith("|")
    }
    linked = {name for name in linked if name.endswith(".md")}
    assert linked <= listed, f"docs/README.md lists pages the site nav does not: {linked - listed}"


def test_the_landing_page_is_the_project_readme(site: Path) -> None:
    html = (site / "index.html").read_text(encoding="utf-8")
    assert "YAML coordinates" in html, "the site's front page is not the project README"


def test_the_changelog_is_published_on_the_site(site: Path) -> None:
    page = site / "changelog" / "index.html"
    assert page.is_file(), "the changelog is not part of the site"
    assert "1.0.0" in page.read_text(encoding="utf-8")


def test_every_internal_link_in_the_built_site_resolves(site: Path) -> None:
    """Walk the built HTML: every local href/src is a file in the site, anchors included."""
    problems: list[str] = []
    pages = sorted(site.rglob("*.html"))
    assert len(pages) > 10, "the site has suspiciously few pages"
    cache: dict[Path, set[str]] = {}
    base_path = urlsplit(config()["site_url"]).path.rstrip("/")
    for page in pages:
        html = page.read_text(encoding="utf-8")
        for raw in links_of(html):
            if raw.startswith(("http://", "https://", "mailto:", "data:", "//")):
                continue
            if raw.startswith("/"):
                # 404.html links from the site root (``/rayspec-py/…``) because it is served for
                # any path; resolve those against the built tree the same way the server does.
                path, _, fragment = unquote(raw).partition("#")
                dest = site / path[len(base_path) :].lstrip("/")
            else:
                target, fragment = urldefrag(urljoin(page.as_uri(), unquote(raw)))
                if not target.startswith("file://"):
                    continue
                dest = Path(target[len("file://") :])
            if dest.is_dir():
                dest = dest / "index.html"
            if not dest.exists():
                problems.append(f"{page.relative_to(site)}: {raw} → missing {dest}")
                continue
            if fragment and dest.suffix == ".html":
                if dest not in cache:
                    cache[dest] = anchors_of(dest.read_text(encoding="utf-8"))
                if fragment not in cache[dest]:
                    problems.append(f"{page.relative_to(site)}: {raw} → no #{fragment}")
    assert not problems, "\n".join(problems)


def test_links_that_leave_the_docs_tree_point_at_the_repository(site: Path) -> None:
    """``concepts.md`` links ``../examples/…`` and ``schema.md`` links ``../schemas``.

    Neither target is part of the site, so on the site they have to become full repository URLs
    — the same page on GitHub keeps the relative link that works there.
    """
    base = "https://github.com/rayspec-labs/rayspec-py/tree/main"
    concepts = (site / "concepts" / "index.html").read_text(encoding="utf-8")
    assert f"{base}/examples/secret_via_tool" in concepts
    schema = (site / "schema" / "index.html").read_text(encoding="utf-8")
    assert f"{base}/schemas" in schema


#: ``[ref]: target`` at the start of a line — a link-reference definition.
LINK_DEFINITION = re.compile(r"^\[[^\]]+\]:\s*(\S+)")
_FENCE = re.compile(r"^\s*(```|~~~)")


def test_no_published_page_links_a_checkout_path_by_reference() -> None:
    """The link rewriter reads inline links only, and that limitation has to be enforced.

    A reference-style definition naming a relative path resolves on GitHub and 404s on the site,
    and the strict build cannot catch it: mkdocs validates the rewritten inline form, never the
    definition line. Use an inline link until ``scripts/mkdocs_hooks.py`` handles both.
    """
    sources = [
        *sorted(DOCS_DIR.glob("*.md")),
        REPO_ROOT / "README.md",
        REPO_ROOT / "CHANGELOG.md",
    ]
    problems = []
    for path in sources:
        fence: str | None = None
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            marker = _FENCE.match(line)
            if marker and fence is None:
                fence = marker.group(1)
                continue
            if marker and marker.group(1) == fence:
                fence = None
                continue
            if fence is not None:
                continue
            found = LINK_DEFINITION.match(line)
            if found and not found.group(1).startswith(
                ("http://", "https://", "mailto:", "#", "/")
            ):
                problems.append(f"{path.name}:{number}: {line.strip()}")
    assert not problems, "reference-style links to checkout paths break on the site:\n" + "\n".join(
        problems
    )


def hooks() -> ModuleType:
    """``scripts/mkdocs_hooks.py``, loaded from its path the way ``mkdocs.yml`` loads it.

    It is a build-time hook rather than an importable module of the package, so there is no
    ``scripts`` on ``sys.path`` to import it from.
    """
    spec = importlib.util.spec_from_file_location(
        "rayspec_mkdocs_hooks", REPO_ROOT / "scripts" / "mkdocs_hooks.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_link_pointing_out_of_the_checkout_is_left_for_the_strict_build_to_report(
    tmp_path: Path,
) -> None:
    """The rewriter turns a checkout path into a repository URL, and only a checkout path.

    A target outside the checkout has no ``blob/main`` URL, so building one raises: the link
    ``[x](../../sibling/README.md)`` crashed the whole build with a bare ``ValueError`` on a
    machine where that sibling happened to exist, and returned the target untouched on one where
    it did not. Both are the same mistake and it has to report the same way — as the unrecognized
    relative link mkdocs already names, with the page and the target in the message.
    """
    outside = tmp_path / "sibling" / "README.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("# outside the checkout, and it exists\n", encoding="utf-8")
    target = os.path.relpath(outside, DOCS_DIR)
    assert Path(DOCS_DIR, target).resolve().is_file(), "the case needs a target that exists"

    left_alone = hooks().rewrite_target(
        target,
        source_dir=DOCS_DIR,
        docs_dir=DOCS_DIR,
        repo_url="https://github.com/rayspec-labs/rayspec-py",
    )
    assert left_alone == target


def test_the_site_is_not_written_into_the_checkout() -> None:
    """``mkdocs build`` defaults to ``site/`` — it must be ignored, never committed."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "site/" in ignored


def test_the_docs_group_installs_the_builder() -> None:
    """``uv sync --all-groups`` has to be enough to build the site locally."""
    import tomllib

    meta = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    docs_group = meta["dependency-groups"]["docs"]
    assert any(dep.startswith("mkdocs") for dep in docs_group)
