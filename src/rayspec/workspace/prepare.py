# SPDX-License-Identifier: Apache-2.0
"""``prepare_workspace`` — the single entry point the engine / CLI call before a run.

Boundary: composes :mod:`project`, :mod:`worktrees`, :mod:`repos` and :mod:`lock` into one
:class:`Workspace` value. It never touches run state; the engine records the fields it needs
(``run.json`` ``workspace{isolation, workdir, branch, base_branch, base_sha, head_sha}``).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

import anyio.to_thread

from rayspec.config import Config
from rayspec.workspace import git as _git
from rayspec.workspace.errors import WorkspaceError
from rayspec.workspace.lock import PathLock
from rayspec.workspace.project import Project, project_from_root
from rayspec.workspace.repos import default_base, remote_tracking_ref, resolve_source
from rayspec.workspace.worktrees import create_worktree

Isolation = Literal["worktree", "none"]
_ISOLATIONS: tuple[str, ...] = ("worktree", "none")


@dataclass(frozen=True, slots=True)
class Workspace:
    """Where a run executes.

    ``isolation`` is ``"worktree"`` or ``"none"``; ``workdir`` is the run's working directory
    (the worktree, or ``project_root`` in place); ``project_root`` is where workflows/agents/
    prompts are loaded from (for ``--repo <url>`` that is the worktree itself). ``branch`` /
    ``base_branch`` / ``base_sha`` / ``head_sha`` mirror ``run.json``'s ``workspace`` block.
    Extras: ``slug`` (project slug), ``notice`` (a one-line message for the console, e.g. a
    non-git directory ran in place) and ``source_root`` (the bare ``source.git`` for URL repos).
    """

    isolation: Isolation
    project_root: Path
    workdir: Path
    branch: str | None
    base_branch: str | None
    base_sha: str | None
    head_sha: str | None
    slug: str = ""
    notice: str | None = None
    source_root: Path | None = None


def _in_place(project: Project, *, notice: str | None, base: str | None = None) -> Workspace:
    """Run in ``project.root`` itself; a ``base`` has no effect and is reported in ``notice``."""
    branch = _git.current_branch(project.root) if project.is_git else None
    head = (
        _git.rev_parse(project.root)
        if project.is_git and _git.ref_exists(project.root, "HEAD")
        else None
    )
    if base is not None:
        ignored = f"base {base!r} ignored: running in place (no worktree)"
        notice = f"{notice}; {ignored}" if notice else ignored
    return Workspace(
        isolation="none",
        project_root=project.root,
        workdir=project.root,
        branch=branch,
        base_branch=None,
        base_sha=None,
        head_sha=head,
        slug=project.slug,
        notice=notice,
    )


def _subdir_of(root: Path) -> Path:
    """``root`` relative to its git top level (``Path('.')`` when it is the top level itself)."""
    top = _git.toplevel(root)
    if top is None:
        return Path()
    try:
        return root.resolve().relative_to(top)
    except ValueError:
        return Path()


def prepare_workspace(
    project_root: Path,
    *,
    home: Path,
    workflow_name: str,
    run_id: str,
    isolation: Isolation = "worktree",
    base: str | None = None,
    repo_arg: str | None = None,
    config: Config | None = None,
) -> Workspace:
    """Resolve the project and create the run's working directory.

    * ``repo_arg`` (``--repo``): a path / registered project / URL replaces ``project_root``;
      URL sources are bare-cloned and **always** run in a worktree.
    * ``isolation="worktree"`` (default) creates ``rayspec/<wf>-<shortid>`` from ``base``
      (else the current branch; ``origin/HEAD`` for URL sources). When ``project_root`` lies
      below the git top level (``packages/foo/.rayspec`` in a monorepo) the whole repository
      is checked out and ``workdir`` is the matching sub-directory of the worktree.
    * ``isolation="none"`` runs in place; non-git directories always run in place (``notice``).
      A ``base`` given for an in-place run has no effect and is reported in ``notice``.
    * A git repository without commits cannot host a worktree (:class:`WorkspaceError` with a
      hint); in place it runs with ``head_sha=None``.
    """
    if isolation not in _ISOLATIONS:
        raise WorkspaceError(
            f"unknown isolation {isolation!r}", hint="use 'worktree' (default) or 'none'"
        )
    notice: str | None = None
    source_root: Path | None = None
    project: Project
    if repo_arg is not None:
        source = resolve_source(repo_arg, config, home=home)
        if base is None:
            base = default_base(source)
        elif source.kind == "url":
            base = remote_tracking_ref(source.root, base)
        if source.kind == "url":
            source_root = source.root
            project = Project(root=source.root, slug=source.slug, name=source.name, is_git=True)
            if isolation != "worktree":
                notice = f"--repo {repo_arg}: URL sources always run in a worktree"
            isolation = "worktree"
        else:
            project = project_from_root(source.root)
    else:
        project = project_from_root(project_root)

    if not project.is_git:
        return _in_place(
            project,
            notice=f"{project.root} is not a git repository: running in place (no worktree)",
            base=base,
        )
    if isolation == "none":
        return _in_place(project, notice=notice, base=base)

    wt = create_worktree(project, home=home, workflow_name=workflow_name, run_id=run_id, base=base)
    # the project root may sit below the git top level (monorepo: packages/foo/.rayspec); the
    # worktree checks out the whole repository, so the run works in the matching sub-directory
    workdir = wt.path / _subdir_of(project.root)
    return Workspace(
        isolation="worktree",
        project_root=wt.path if source_root is not None else project.root,
        workdir=workdir,
        branch=wt.branch,
        base_branch=wt.base_branch,
        base_sha=wt.base_sha,
        head_sha=wt.head_sha,
        slug=project.slug,
        notice=notice,
        source_root=source_root,
    )


async def prepare_workspace_async(
    project_root: Path,
    *,
    home: Path,
    workflow_name: str,
    run_id: str,
    isolation: Isolation = "worktree",
    base: str | None = None,
    repo_arg: str | None = None,
    config: Config | None = None,
) -> Workspace:
    """:func:`prepare_workspace` on a worker thread (clone/fetch may take a while)."""
    return await anyio.to_thread.run_sync(
        partial(
            prepare_workspace,
            project_root,
            home=home,
            workflow_name=workflow_name,
            run_id=run_id,
            isolation=isolation,
            base=base,
            repo_arg=repo_arg,
            config=config,
        )
    )


def workspace_lock(workspace: Workspace, *, home: Path, run_id: str) -> PathLock:
    """The :class:`PathLock` guarding ``workspace.workdir`` (not yet acquired)."""
    return PathLock(home, workspace.slug, workspace.workdir, run_id=run_id)


__all__ = [
    "Isolation",
    "Workspace",
    "prepare_workspace",
    "prepare_workspace_async",
    "workspace_lock",
]
