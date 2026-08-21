# SPDX-License-Identifier: Apache-2.0
"""Thin, synchronous git helpers (``subprocess``) with error capture.

Boundary: this module shells out to ``git`` and nothing else. Every failure surfaces as
:class:`GitError` (a :class:`RayspecError`), never as a raw ``CalledProcessError``. The helpers
are blocking and fast (local git commands); call them through ``anyio.to_thread.run_sync`` from
async code when latency matters (``fetch`` on a remote may take a while).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from rayspec.workspace.errors import GitError

#: Environment applied to every git call: no prompts, no pager, stable output.
_GIT_ENV_DEFAULTS: Mapping[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "LC_ALL": "C",
}


@dataclass(frozen=True, slots=True)
class GitResult:
    """Outcome of one git invocation (``stdout``/``stderr`` are stripped of trailing newlines)."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def git_executable() -> str:
    """Path of the ``git`` binary (``GitError`` with a hint when it is not installed)."""
    exe = shutil.which("git")
    if exe is None:
        raise GitError("git is not installed or not on PATH", hint="install git and retry")
    return exe


def run_git(
    args: Sequence[str],
    cwd: Path | str | None,
    *,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> GitResult:
    """Run ``git <args>`` in ``cwd`` and return a :class:`GitResult`.

    With ``check=True`` (default) a non-zero exit raises :class:`GitError` whose message carries
    the command and the captured stderr. ``cwd=None`` runs in the current directory.
    """
    cmd = [git_executable(), *args]
    full_env = {**os.environ, **_GIT_ENV_DEFAULTS, **(env or {})}
    try:
        proc = subprocess.run(
            cmd,
            cwd=None if cwd is None else str(cwd),
            capture_output=True,
            text=True,
            env=full_env,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # cwd vanished
        raise GitError(f"cannot run git in {cwd}: {exc}", args=tuple(args)) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git {' '.join(args)} timed out after {timeout}s", args=tuple(args)
        ) from exc
    result = GitResult(
        args=tuple(args),
        returncode=proc.returncode,
        stdout=proc.stdout.rstrip("\n"),
        stderr=proc.stderr.rstrip("\n"),
    )
    if check and not result.ok:
        where = f" (in {cwd})" if cwd is not None else ""
        detail = result.stderr or result.stdout
        raise GitError(
            f"git {' '.join(args)} failed{where}: {detail or f'exit {result.returncode}'}",
            args=result.args,
            returncode=result.returncode,
            stderr=result.stderr,
        )
    return result


def is_git_repo(path: Path) -> bool:
    """True when ``path`` is inside a git work tree or is a bare repository."""
    if not path.is_dir():
        return False
    res = run_git(["rev-parse", "--is-inside-work-tree"], path, check=False)
    if res.ok and res.stdout.strip() == "true":
        return True
    res = run_git(["rev-parse", "--is-bare-repository"], path, check=False)
    return res.ok and res.stdout.strip() == "true"


def toplevel(path: Path) -> Path | None:
    """The work-tree root containing ``path`` (``None`` outside a work tree / for bare repos)."""
    if not path.is_dir():
        return None
    res = run_git(["rev-parse", "--show-toplevel"], path, check=False)
    if not res.ok or not res.stdout.strip():
        return None
    return Path(res.stdout.strip()).resolve()


def common_dir(path: Path) -> Path:
    """The repository's common git dir (shared by all worktrees), absolute."""
    res = run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    return Path(res.stdout.strip()).resolve()


def current_branch(path: Path) -> str | None:
    """Short name of the checked-out branch, ``None`` when HEAD is detached."""
    res = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], path, check=False)
    if not res.ok:
        return None
    return res.stdout.strip() or None


def rev_parse(path: Path, ref: str = "HEAD") -> str:
    """Resolve ``ref`` to a full commit sha (``GitError`` for unknown refs)."""
    res = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], path, check=False)
    if not res.ok or not res.stdout.strip():
        raise GitError(
            f"unknown git revision {ref!r} in {path}",
            args=res.args,
            returncode=res.returncode,
            stderr=res.stderr,
            hint="pass an existing branch, tag or commit as --base",
        )
    return res.stdout.strip()


def ref_exists(path: Path, ref: str) -> bool:
    """True when ``ref`` resolves to a commit."""
    res = run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], path, check=False)
    return res.ok and bool(res.stdout.strip())


def branch_exists(path: Path, branch: str) -> bool:
    """True when the local branch ``branch`` exists."""
    res = run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], path, check=False)
    return res.ok


def is_dirty(path: Path, *, untracked: bool = True) -> bool:
    """True when the work tree has uncommitted changes (untracked files count by default)."""
    args = ["status", "--porcelain"]
    args.append("--untracked-files=normal" if untracked else "--untracked-files=no")
    res = run_git(args, path)
    return bool(res.stdout.strip())


def remote_url(path: Path, remote: str = "origin") -> str | None:
    """The fetch URL of ``remote`` (``None`` when the remote is not configured)."""
    res = run_git(["remote", "get-url", remote], path, check=False)
    if not res.ok:
        return None
    return res.stdout.strip() or None


def remote_default_branch(path: Path, remote: str = "origin") -> str | None:
    """``origin/HEAD`` as ``<remote>/<branch>`` (e.g. ``origin/main``); ``None`` when unset."""
    res = run_git(
        ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"], path, check=False
    )
    if res.ok and res.stdout.strip():
        return res.stdout.strip()
    return None


def set_remote_head(path: Path, remote: str = "origin") -> str | None:
    """Refresh ``<remote>/HEAD`` from the remote (``git remote set-head -a``); returns it."""
    run_git(["remote", "set-head", remote, "--auto"], path, check=False)
    return remote_default_branch(path, remote)


def fetch_prune(path: Path, remote: str = "origin", *, timeout: float | None = None) -> None:
    """``git fetch --prune <remote>``."""
    run_git(["fetch", "--prune", "--quiet", remote], path, timeout=timeout)


def is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant`` (``merge-base --is-ancestor``)."""
    res = run_git(["merge-base", "--is-ancestor", ancestor, descendant], path, check=False)
    if res.returncode == 0:
        return True
    if res.returncode == 1:
        return False
    raise GitError(
        f"git merge-base --is-ancestor {ancestor} {descendant} failed: {res.stderr}",
        args=res.args,
        returncode=res.returncode,
        stderr=res.stderr,
    )


__all__ = [
    "GitResult",
    "branch_exists",
    "common_dir",
    "current_branch",
    "fetch_prune",
    "git_executable",
    "is_ancestor",
    "is_dirty",
    "is_git_repo",
    "ref_exists",
    "remote_default_branch",
    "remote_url",
    "rev_parse",
    "run_git",
    "set_remote_head",
    "toplevel",
]
