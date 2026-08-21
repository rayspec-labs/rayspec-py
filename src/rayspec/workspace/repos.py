# SPDX-License-Identifier: Apache-2.0
"""``--repo`` sources: a local path, a registered project (``config.projects``) or a git URL.

Boundary: URL sources are cloned **bare** into ``<home>/projects/<slug>/source.git`` and kept
fresh with ``git fetch --prune``; nothing is ever checked out inside ``source.git`` — callers
always create a worktree from it (:func:`rayspec.workspace.prepare.prepare_workspace`).
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from rayspec.config import Config
from rayspec.store.file import secure_mkdir
from rayspec.workspace import git as _git
from rayspec.workspace.errors import GitError, WorkspaceError
from rayspec.workspace.project import (
    normalize_remote_url,
    project_dir,
    project_from_root,
)

#: Name of the bare clone inside the project directory.
SOURCE_DIR_NAME = "source.git"
#: Fetch refspec of the bare source: upstream branches live under ``refs/remotes/origin/*``.
REMOTE_FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?[^:/\s]+:[^/].*$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True, slots=True)
class RepoSource:
    """A resolved ``--repo`` argument.

    ``kind`` is ``"path"`` (``root`` is a checkout used as the project root) or ``"url"``
    (``root`` is the bare ``source.git``; always run in a worktree). ``base`` is the registered
    project's default base (if any); ``project_name`` the registered name it resolved through.
    """

    kind: Literal["path", "url"]
    arg: str
    root: Path
    slug: str
    name: str
    url: str | None = None
    base: str | None = None
    project_name: str | None = None


def is_git_url(text: str) -> bool:
    """True for ``scheme://...`` URLs and ``[user@]host:path`` scp-style remotes."""
    text = text.strip()
    if not text or _WINDOWS_DRIVE_RE.match(text):
        return False
    if _SCHEME_RE.match(text):
        return True
    return bool(_SCP_RE.match(text)) and not text.startswith((".", "/", "~"))


def _looks_like_path(text: str) -> bool:
    return text.startswith((".", "/", "~")) or "/" in text or "\\" in text


def source_slug(url: str) -> str:
    """Slug for a URL source: ``host/owner/repo`` or ``local/<name>-<sha1(url)[:8]>``."""
    slug = normalize_remote_url(url)
    if slug:
        return slug
    name = _url_basename(url)
    digest = hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:8]
    return f"local/{name}-{digest}"


def _url_basename(url: str) -> str:
    path = urlsplit(url).path if "://" in url else url.rsplit(":", 1)[-1]
    name = unquote(path).rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name or "repo"


def source_dir(home: Path, slug: str) -> Path:
    """``<home>/projects/<slug>/source.git``."""
    return project_dir(home, slug) / SOURCE_DIR_NAME


def _init_bare_source(url: str, dest: Path, *, timeout: float | None) -> None:
    """``git init --bare`` + ``remote add origin`` + first fetch (no local ``refs/heads/*``).

    A ``git clone --bare`` would snapshot every upstream branch into ``refs/heads/*`` and
    later ``fetch --prune`` only moves ``refs/remotes/origin/*`` — bare branch names would then
    resolve to the clone-time tip forever. Starting from an empty bare repo keeps the local
    namespace for ``rayspec/*`` branches only. On failure ``dest`` is removed again.
    """
    secure_mkdir(dest.parent)  # <home>/projects/<slug> 0700; the bare repo is git's
    try:
        _git.run_git(["init", "--bare", "--quiet", str(dest)], None)
        _git.run_git(["remote", "add", "origin", url], dest)
        _git.run_git(["config", "remote.origin.fetch", REMOTE_FETCH_REFSPEC], dest)
        _git.fetch_prune(dest, timeout=timeout)
    except GitError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise WorkspaceError(
            f"could not clone {url}: {exc.stderr or exc}",
            hint="check the URL and your git credentials (network access is required)",
        ) from exc


def ensure_bare_source(
    url: str, *, home: Path, fetch: bool = True, timeout: float | None = None
) -> Path:
    """Create the bare ``source.git`` for ``url`` (first use) or ``git fetch --prune`` it.

    The bare repo only tracks ``refs/remotes/origin/*`` (so ``origin/HEAD``/``origin/<branch>``
    are always the fetched tips and local ``rayspec/*`` branches survive pruning); it never has
    local copies of upstream branches. Returns the bare repo path.
    """
    dest = source_dir(home, source_slug(url))
    if not dest.is_dir():
        _init_bare_source(url, dest, timeout=timeout)
        fetch = False  # just fetched
    if fetch:
        try:
            _git.fetch_prune(dest, timeout=timeout)
        except GitError as exc:
            raise WorkspaceError(
                f"could not fetch {url} into {dest}: {exc.stderr or exc}",
                hint="check the network / credentials, or delete source.git to re-clone",
            ) from exc
    _git.set_remote_head(dest)
    return dest


def remote_tracking_ref(root: Path, ref: str) -> str:
    """Map a bare branch name to its ``origin/<name>`` tracking ref in a bare source.

    ``main`` → ``origin/main`` when ``refs/remotes/origin/main`` exists (even if a stale local
    ``refs/heads/main`` from an older ``clone --bare`` is present); anything else (``origin/x``,
    tags, shas, ``rayspec/*`` branches) is returned unchanged.
    """
    ref = ref.strip()
    if not ref or ref.startswith(("origin/", "refs/")):
        return ref
    if _git.ref_exists(root, f"refs/remotes/origin/{ref}"):
        return f"origin/{ref}"
    return ref


def _resolve_path(
    arg: str, text: str, *, cwd: Path, base: str | None, project_name: str | None
) -> RepoSource:
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = cwd / path
    path = path.resolve()
    if not path.is_dir():
        raise WorkspaceError(
            f"--repo {arg!r}: {path} is not a directory",
            hint="pass a local checkout, a registered project name or a git URL",
        )
    project = project_from_root(path)  # non-git dirs are allowed: they run in place
    return RepoSource(
        kind="path",
        arg=arg,
        root=project.root,
        slug=project.slug,
        name=project.name,
        base=base,
        project_name=project_name,
    )


def _resolve_url(
    arg: str,
    url: str,
    *,
    home: Path,
    base: str | None,
    project_name: str | None,
    fetch: bool,
) -> RepoSource:
    root = ensure_bare_source(url, home=home, fetch=fetch)
    slug = source_slug(url)
    return RepoSource(
        kind="url",
        arg=arg,
        root=root,
        slug=slug,
        name=slug.rsplit("/", 1)[-1] if not slug.startswith("local/") else _url_basename(url),
        url=url,
        base=base,
        project_name=project_name,
    )


def resolve_source(
    arg: str,
    config: Config | None,
    *,
    home: Path,
    cwd: Path | None = None,
    fetch: bool = True,
) -> RepoSource:
    """Resolve a ``--repo`` argument (``fetch=False`` skips ``git fetch`` on an existing clone).

    Order: an explicit path form (``./x``, ``/x``, ``~/x``, anything with a separator) → that
    checkout; a registered project name (``config.projects``) → its source (path or URL) with
    its ``base``; a git URL → bare clone/fetch; a bare name that is an existing directory →
    that checkout; else :class:`WorkspaceError`.
    """
    text = arg.strip()
    here = (Path.cwd() if cwd is None else cwd).resolve()
    if not text:
        raise WorkspaceError("--repo needs a path, registered project name or git URL")
    if not is_git_url(text) and _looks_like_path(text):
        return _resolve_path(arg, text, cwd=here, base=None, project_name=None)
    if config is not None:
        for spec in config.projects:
            if spec.name == text:
                if is_git_url(spec.source):
                    return _resolve_url(
                        arg,
                        spec.source,
                        home=home,
                        base=spec.base,
                        project_name=spec.name,
                        fetch=fetch,
                    )
                return _resolve_path(
                    arg, spec.source, cwd=here, base=spec.base, project_name=spec.name
                )
    if is_git_url(text):
        return _resolve_url(arg, text, home=home, base=None, project_name=None, fetch=fetch)
    if (here / text).is_dir():
        return _resolve_path(arg, text, cwd=here, base=None, project_name=None)
    names = sorted(p.name for p in config.projects) if config else []
    hint = (
        f"registered projects: {', '.join(names)}"
        if names
        else "rayspec projects add <name> <source>"
    )
    raise WorkspaceError(
        f"--repo {arg!r}: not a directory, registered project or git URL", hint=hint
    )


def default_base(source: RepoSource) -> str | None:
    """The base ref for a source when ``--base`` is absent.

    The registered ``base`` wins (mapped to ``origin/<base>`` for URL sources); else URL sources
    use ``origin/HEAD`` (``origin/<default>``) or the bare repo's HEAD branch mapped the same
    way; path sources return ``None`` (= the checkout's current branch).
    """
    if source.kind != "url":
        return source.base or None
    base = (
        source.base or _git.remote_default_branch(source.root) or _git.current_branch(source.root)
    )
    return remote_tracking_ref(source.root, base) if base else None


__all__ = [
    "REMOTE_FETCH_REFSPEC",
    "SOURCE_DIR_NAME",
    "RepoSource",
    "default_base",
    "ensure_bare_source",
    "is_git_url",
    "remote_tracking_ref",
    "resolve_source",
    "source_dir",
    "source_slug",
]
