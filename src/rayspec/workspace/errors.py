# SPDX-License-Identifier: Apache-2.0
"""Workspace-layer exceptions (all derive from :class:`rayspec.errors.RayspecError`)."""

from __future__ import annotations

from rayspec.errors import RayspecError


class WorkspaceError(RayspecError):
    """Base class for project / worktree / repo-source / lock failures."""


class GitError(WorkspaceError):
    """A git command failed; ``args``, ``returncode`` and ``stderr`` describe the failure."""

    def __init__(
        self,
        message: str,
        *,
        args: tuple[str, ...] = (),
        returncode: int | None = None,
        stderr: str = "",
        hint: str | None = None,
    ):
        super().__init__(message, hint=hint)
        self.git_args = args
        self.returncode = returncode
        self.stderr = stderr


class WorkdirLockedError(WorkspaceError):
    """Another run holds the path lock for this workdir (``run_id`` / ``pid`` of the holder)."""

    def __init__(
        self,
        message: str,
        *,
        workdir: str,
        run_id: str | None = None,
        pid: int | None = None,
        hint: str | None = None,
    ):
        super().__init__(message, hint=hint)
        self.workdir = workdir
        self.run_id = run_id
        self.pid = pid


__all__ = ["GitError", "WorkdirLockedError", "WorkspaceError"]
