# SPDX-License-Identifier: Apache-2.0
"""Project discovery: git top level, remote-derived slug and the per-project home directory.

Boundary: pure path/URL logic plus the read-only git helpers in :mod:`rayspec.workspace.git`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from rayspec.workspace import git as _git

#: ``scp``-style remotes: ``[user@]host:path`` (no scheme, no leading slash, no Windows drive).
_SCP_RE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/\s]+):(?P<path>[^/].*)$")
_SCHEMES = frozenset({"ssh", "git", "http", "https", "git+ssh", "ssh+git"})


@dataclass(frozen=True, slots=True)
class Project:
    """A project: its root directory, remote-derived ``slug`` (``host/owner/repo`` or
    ``local/<dir>-<hash>``), display ``name`` and whether it is a git repository."""

    root: Path
    slug: str
    name: str
    is_git: bool


def _clean_path(path: str) -> str | None:
    path = unquote(path).strip().strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    if not path or "/" not in path:
        return None
    segments = [s for s in path.split("/") if s and s not in {".", ".."}]
    if len(segments) < 2:
        return None
    return "/".join(segments)


def normalize_remote_url(url: str) -> str | None:
    """Normalise a git remote URL to ``host/owner/repo`` (host lower-cased, ``.git`` dropped,
    percent-encoding in the path decoded).

    Accepts ``git@host:o/r.git``, ``ssh://[user@]host[:port]/o/r``, ``https://host/o/r[.git]``,
    ``git://host/o/r``. Local paths, ``file://`` URLs and unparseable strings return ``None``
    (callers fall back to the ``local/...`` slug).
    """
    url = url.strip()
    if not url:
        return None
    if "://" in url:
        parts = urlsplit(url)
        if parts.scheme.lower() not in _SCHEMES or not parts.hostname:
            return None
        path = _clean_path(parts.path)
        if path is None:
            return None
        return f"{parts.hostname.lower()}/{path}"
    match = _SCP_RE.match(url)
    if match is None:
        return None
    path = _clean_path(match.group("path"))
    if path is None:
        return None
    return f"{match.group('host').lower()}/{path}"


def local_slug(root: Path) -> str:
    """``local/<dirname>-<sha1(abspath)[:8]>`` for projects without a usable remote."""
    resolved = root.resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    name = resolved.name or "root"
    return f"local/{name}-{digest}"


def project_slug(root: Path) -> str:
    """Slug for the project at ``root``: normalised ``origin`` URL, else :func:`local_slug`."""
    if _git.is_git_repo(root):
        url = _git.remote_url(root, "origin")
        if url:
            slug = normalize_remote_url(url)
            if slug:
                return slug
    return local_slug(root)


def project_from_root(root: Path) -> Project:
    """Build the :class:`Project` for ``root`` (no discovery; ``root`` is taken as-is)."""
    resolved = root.resolve()
    is_git = _git.is_git_repo(resolved)
    slug = project_slug(resolved)
    name = slug.rsplit("/", 1)[-1] if not slug.startswith("local/") else resolved.name
    return Project(root=resolved, slug=slug, name=name or slug, is_git=is_git)


def discover_project(cwd: Path | None = None) -> Project:
    """The git project containing ``cwd`` (default: the process cwd).

    The root is the git top level; a directory that is not in a git repository is its own root
    (``project_slug`` then mints the ``local/...`` slug). This is a *git* question, which is why
    it is not :func:`rayspec.loader.find_project_root`: that one answers "which directory holds
    the ``.rayspec/`` a command reads", and in a repository whose project lives in
    ``packages/foo/.rayspec`` the two answers differ on purpose.
    """
    start = (Path.cwd() if cwd is None else cwd).resolve()
    return project_from_root(_git.toplevel(start) or start)


def project_dir(home: Path, slug: str) -> Path:
    """``<home>/projects/<slug>`` — the per-project state directory."""
    return home / "projects" / Path(*slug.split("/"))


__all__ = [
    "Project",
    "discover_project",
    "local_slug",
    "normalize_remote_url",
    "project_dir",
    "project_from_root",
    "project_slug",
]
