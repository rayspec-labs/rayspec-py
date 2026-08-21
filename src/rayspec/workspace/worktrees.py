# SPDX-License-Identifier: Apache-2.0
"""Worktree lifecycle: create (default isolation), list, remove and clean ``rayspec/*`` worktrees.

Boundary: git worktree commands via :mod:`rayspec.workspace.git`; path layout from
:mod:`rayspec.workspace.project`. No run-state knowledge (the engine records what it needs).
Directories this module creates under ``$RAYSPEC_HOME`` are private (``0700``, the store's
``secure_mkdir``); the checkout content itself is git's and keeps git's modes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rayspec.store.file import secure_mkdir
from rayspec.workspace import git as _git
from rayspec.workspace.errors import GitError, WorkspaceError
from rayspec.workspace.lock import remove_lock_file
from rayspec.workspace.project import Project, project_dir

#: Branch namespace every rayspec worktree lives in.
BRANCH_PREFIX = "rayspec/"

_AGE_UNITS = {"w": 7 * 86400, "d": 86400, "h": 3600, "m": 60, "s": 1}
_AGE_RE = re.compile(r"(\d+)([wdhms])")


@dataclass(frozen=True, slots=True)
class Worktree:
    """A freshly created worktree: checkout ``path`` on ``branch`` started at ``base_sha``."""

    path: Path
    branch: str
    base_branch: str | None
    base_sha: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    """One existing ``rayspec/*`` worktree as reported by ``git worktree list``."""

    path: Path
    branch: str
    head_sha: str
    created_at: datetime | None
    dirty: bool
    merged: bool
    prunable: bool = False
    locked: bool = False

    @property
    def age(self) -> timedelta | None:
        """Time since creation (``None`` when the checkout directory is gone)."""
        if self.created_at is None:
            return None
        return datetime.now(UTC) - self.created_at


@dataclass(slots=True)
class CleanReport:
    """Outcome of :func:`clean_worktrees`: what was (or would be) removed and what was skipped."""

    removed: list[WorktreeInfo] = field(default_factory=list)
    skipped: list[tuple[WorktreeInfo, str]] = field(default_factory=list)
    dry_run: bool = False


def parse_age(text: str) -> timedelta:
    """Parse ``7d`` / ``12h`` / ``30m`` / ``45s`` / ``2w`` (combinable: ``1d12h``) to a delta."""
    raw = text.strip().lower()
    if raw == "0":
        return timedelta(0)
    if not raw or not re.fullmatch(r"(?:\d+[wdhms])+", raw):
        raise ValueError(
            f"invalid age {text!r}; use <n>w, <n>d, <n>h, <n>m or <n>s (e.g. 7d, 12h, 1d12h)"
        )
    seconds = sum(int(n) * _AGE_UNITS[unit] for n, unit in _AGE_RE.findall(raw))
    return timedelta(seconds=seconds)


def short_run_id(run_id: str) -> str:
    """The short id used in worktree/branch names: the last ``-`` segment of the run id."""
    return run_id.rsplit("-", 1)[-1] or run_id


def worktree_name(workflow_name: str, run_id: str) -> str:
    """``<workflow>-<shortid>`` — directory and branch suffix."""
    return f"{workflow_name}-{short_run_id(run_id)}"


def worktree_branch(workflow_name: str, run_id: str) -> str:
    """``rayspec/<workflow>-<shortid>``."""
    return BRANCH_PREFIX + worktree_name(workflow_name, run_id)


def worktrees_dir(home: Path, slug: str) -> Path:
    """``<home>/projects/<slug>/worktrees``."""
    return project_dir(home, slug) / "worktrees"


def _resolve_base(root: Path, base: str | None) -> tuple[str, str | None]:
    """Return ``(ref to start from, base branch name or None)``."""
    if base is not None:
        if not _git.ref_exists(root, base):
            raise WorkspaceError(
                f"base {base!r} is not a branch, tag or commit in {root}",
                hint="pass an existing ref as --base (e.g. main or origin/main)",
            )
        return base, base
    if not _git.ref_exists(root, "HEAD"):
        raise WorkspaceError(
            f"{root} has no commits yet; cannot create a worktree",
            hint="commit something first, or run with --no-worktree (isolation: none)",
        )
    branch = _git.current_branch(root)
    if branch is not None:
        return branch, branch
    return "HEAD", None


def create_worktree(
    project: Project,
    *,
    home: Path,
    workflow_name: str,
    run_id: str,
    base: str | None = None,
) -> Worktree:
    """``git worktree add -b rayspec/<wf>-<id> <home>/projects/<slug>/worktrees/<wf>-<id> <base>``.

    ``base`` defaults to the project's current branch (``HEAD`` when detached). When the short
    name is already taken (branch or directory) the full run id is used instead.
    """
    if not project.is_git:
        raise WorkspaceError(
            f"{project.root} is not a git repository; cannot create a worktree",
            hint="run with --no-worktree (isolation: none) or git init the project",
        )
    start_ref, base_branch = _resolve_base(project.root, base)
    base_sha = _git.rev_parse(project.root, start_ref)
    wt_dir = worktrees_dir(home, project.slug)
    secure_mkdir(wt_dir)  # new dirs 0700; the checkout below keeps git's modes
    name = worktree_name(workflow_name, run_id)
    branch = BRANCH_PREFIX + name
    path = wt_dir / name
    if path.exists() or _git.branch_exists(project.root, branch):
        name = f"{workflow_name}-{run_id}"
        branch = BRANCH_PREFIX + name
        path = wt_dir / name
        if path.exists() or _git.branch_exists(project.root, branch):
            raise WorkspaceError(
                f"worktree {path} or branch {branch} already exists",
                hint="rayspec worktrees clean, or pick another run id",
            )
    _git.run_git(
        ["worktree", "add", "--quiet", "--no-track", "-b", branch, str(path), start_ref],
        project.root,
    )
    return Worktree(
        path=path, branch=branch, base_branch=base_branch, base_sha=base_sha, head_sha=base_sha
    )


def recreate_worktree(project: Project, *, path: Path, branch: str) -> Worktree:
    """Re-attach an existing ``rayspec/*`` branch at ``path`` (resume after the checkout vanished).

    Prunes stale worktree entries first, then ``git worktree add <path> <branch>``. The branch
    must exist; ``base_sha``/``head_sha`` are its current tip.
    """
    if not _git.branch_exists(project.root, branch):
        raise WorkspaceError(
            f"branch {branch} is missing in {project.root}; cannot recreate the worktree",
            hint="re-run without --resume (a fresh worktree is created) or use --force",
        )
    _git.run_git(["worktree", "prune"], project.root)
    secure_mkdir(path.parent)
    _git.run_git(["worktree", "add", "--quiet", str(path), branch], project.root)
    head = _git.rev_parse(path)
    return Worktree(path=path, branch=branch, base_branch=None, base_sha=head, head_sha=head)


def _parse_porcelain(text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(current)
    return entries


def _merge_target(root: Path, merged_into: str | None) -> str | None:
    """The ref ``merged`` is computed against, validated once.

    An explicit ``merged_into`` must resolve (else :class:`WorkspaceError`); the default is
    ``origin/HEAD`` when it resolves, else ``HEAD``; ``None`` when even HEAD is unborn (every
    worktree then counts as unmerged).
    """
    if merged_into is not None:
        if not _git.ref_exists(root, merged_into):
            raise WorkspaceError(
                f"merge target {merged_into!r} is not a branch, tag or commit in {root}",
                hint="pass an existing ref as --merged-into (e.g. main or origin/main)",
            )
        return merged_into
    default = _git.remote_default_branch(root)
    if default is not None and _git.ref_exists(root, default):
        return default
    return "HEAD" if _git.ref_exists(root, "HEAD") else None


def _created_at(path: Path) -> datetime | None:
    """Creation time heuristic: the older mtime of the worktree's ``.git`` pointer file and of
    git's admin ``<common>/worktrees/<id>/gitdir`` file (both written once by ``worktree add``;
    ``git worktree move|repair`` rewrites them, which resets the age — documented)."""
    marker = path / ".git"
    candidates = [marker]
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if text.startswith("gitdir:"):
        candidates.append(Path(text.partition(":")[2].strip()) / "gitdir")
    stamps: list[float] = []
    for candidate in candidates:
        try:
            stamps.append(candidate.stat().st_mtime)
        except OSError:
            continue
    if not stamps:
        return None
    return datetime.fromtimestamp(min(stamps), tz=UTC)


def list_worktrees(project: Project, *, merged_into: str | None = None) -> list[WorktreeInfo]:
    """All worktrees of ``project`` on ``rayspec/*`` branches, sorted by branch name.

    ``merged`` is computed against ``merged_into`` (default: ``origin/HEAD`` when it resolves,
    else the project's ``HEAD``; an unknown explicit ``merged_into`` is a :class:`WorkspaceError`);
    ``dirty`` via ``git status`` inside the worktree.
    """
    if not project.is_git:
        return []
    res = _git.run_git(["worktree", "list", "--porcelain"], project.root)
    target = _merge_target(project.root, merged_into)
    infos: list[WorktreeInfo] = []
    for entry in _parse_porcelain(res.stdout):
        ref = entry.get("branch", "")
        if "bare" in entry or not ref.startswith("refs/heads/" + BRANCH_PREFIX):
            continue
        branch = ref[len("refs/heads/") :]
        path = Path(entry["worktree"])
        head_sha = entry.get("HEAD", "")
        prunable = "prunable" in entry or not path.exists()
        dirty = bool(path.is_dir()) and _git.is_dirty(path)
        merged = (
            bool(head_sha)
            and target is not None
            and _git.is_ancestor(project.root, head_sha, target)
        )
        infos.append(
            WorktreeInfo(
                path=path,
                branch=branch,
                head_sha=head_sha,
                created_at=_created_at(path),
                dirty=dirty,
                merged=merged,
                prunable=prunable,
                locked="locked" in entry,
            )
        )
    infos.sort(key=lambda i: i.branch)
    return infos


def remove_worktree(
    path: Path,
    *,
    delete_branch: bool = True,
    force: bool = False,
    repo: Path | None = None,
    branch: str | None = None,
) -> None:
    """``git worktree remove [--force --force] <path>`` (+ ``git branch -D`` if ``delete_branch``).

    ``force`` removes dirty **and** locked worktrees (git needs ``--force`` twice for locked
    ones). ``repo`` and ``branch`` are discovered from the checkout when it still exists; pass
    them explicitly to clean up an entry whose directory is already gone (``git worktree prune``).
    """
    exists = path.is_dir()
    if repo is None:
        if not exists:
            raise WorkspaceError(
                f"worktree directory {path} is gone; pass repo= to prune its entry",
            )
        repo = _git.common_dir(path)
    if branch is None and exists:
        branch = _git.current_branch(path)
    if exists:
        args = ["worktree", "remove"]
        if force:
            args += ["--force", "--force"]
        _git.run_git([*args, str(path)], repo)
    else:
        _git.run_git(["worktree", "prune"], repo)
    if delete_branch and branch and _git.branch_exists(repo, branch):
        _git.run_git(["branch", "-D", branch], repo)


def clean_worktrees(
    project: Project,
    *,
    older_than: timedelta | None = None,
    merged_only: bool = False,
    force: bool = False,
    dry_run: bool = False,
    merged_into: str | None = None,
    home: Path | None = None,
) -> CleanReport:
    """Remove ``rayspec/*`` worktrees (and their branches) matching the filters.

    With ``home`` the per-workdir lock file of every removed worktree
    (``<home>/projects/<slug>/locks/<sha1(path)>.lock``) is unlinked too, unless a live run
    still holds it.

    Safe by default: without ``force`` only merged, clean, unlocked worktrees are removed;
    unmerged ones (committed work not reachable from the merge target), dirty ones and
    git-locked ones are reported as skipped with the reason. ``older_than`` keeps younger
    worktrees; ``merged_only`` reports unmerged ones as ``not merged`` instead. A failing
    removal is recorded as skipped (``str(exc)``) and the remaining candidates are still
    processed. ``dry_run`` reports without touching anything.
    """
    report = CleanReport(dry_run=dry_run)
    for info in list_worktrees(project, merged_into=merged_into):
        age = info.age
        if older_than is not None and not info.prunable and (age is None or age < older_than):
            report.skipped.append((info, f"younger than {older_than}"))
            continue
        if merged_only and not info.merged:
            report.skipped.append((info, "not merged"))
            continue
        if not force:
            if not info.merged and not info.prunable:
                report.skipped.append((info, "unmerged commits (use --force)"))
                continue
            if info.dirty:
                report.skipped.append((info, "dirty (use --force)"))
                continue
            if info.locked:
                report.skipped.append((info, "locked (use --force)"))
                continue
        if not dry_run:
            try:
                remove_worktree(
                    info.path,
                    delete_branch=True,
                    force=force,
                    repo=project.root,
                    branch=info.branch,
                )
            except GitError as exc:
                report.skipped.append((info, str(exc)))
                continue
            if home is not None:
                remove_lock_file(home, project.slug, info.path)
        report.removed.append(info)
    return report


__all__ = [
    "BRANCH_PREFIX",
    "CleanReport",
    "Worktree",
    "WorktreeInfo",
    "clean_worktrees",
    "create_worktree",
    "list_worktrees",
    "parse_age",
    "recreate_worktree",
    "remove_worktree",
    "short_run_id",
    "worktree_branch",
    "worktree_name",
    "worktrees_dir",
]
