# SPDX-License-Identifier: Apache-2.0
"""Workspace layer: project discovery, git helpers, worktrees, ``--repo`` sources, path lock.

Boundary: everything that touches the file system *around* a run's working directory. Depends
on :mod:`rayspec.config` (registered projects) and :mod:`rayspec.errors` only; never on the
engine, providers or the store.
"""

from rayspec.workspace.errors import GitError, WorkdirLockedError, WorkspaceError
from rayspec.workspace.lock import LockHolder, PathLock, lock_path, read_lock_holder
from rayspec.workspace.prepare import (
    Isolation,
    Workspace,
    prepare_workspace,
    prepare_workspace_async,
    workspace_lock,
)
from rayspec.workspace.project import (
    Project,
    discover_project,
    normalize_remote_url,
    project_dir,
    project_from_root,
    project_slug,
)
from rayspec.workspace.repos import RepoSource, ensure_bare_source, is_git_url, resolve_source
from rayspec.workspace.worktrees import (
    CleanReport,
    Worktree,
    WorktreeInfo,
    clean_worktrees,
    create_worktree,
    list_worktrees,
    parse_age,
    recreate_worktree,
    remove_worktree,
)

__all__ = [
    "CleanReport",
    "GitError",
    "Isolation",
    "LockHolder",
    "PathLock",
    "Project",
    "RepoSource",
    "WorkdirLockedError",
    "Workspace",
    "WorkspaceError",
    "Worktree",
    "WorktreeInfo",
    "clean_worktrees",
    "create_worktree",
    "discover_project",
    "ensure_bare_source",
    "is_git_url",
    "list_worktrees",
    "lock_path",
    "normalize_remote_url",
    "parse_age",
    "prepare_workspace",
    "prepare_workspace_async",
    "project_dir",
    "project_from_root",
    "project_slug",
    "read_lock_holder",
    "recreate_worktree",
    "remove_worktree",
    "resolve_source",
    "workspace_lock",
]
