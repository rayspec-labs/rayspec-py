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

#: Environment variable that opts a run into publishing its branch when it pauses or ends.
#: ``1``/``true``/``yes``/``on`` mean ``origin``; any other non-empty value names the remote.
PUSH_ENV = "RAYSPEC_PUSH_BRANCH"
#: The remote :func:`push_remote` picks when the opt-in is just "on".
DEFAULT_REMOTE = "origin"
#: How long :func:`push_branch` waits for git before it gives up (and warns).
PUSH_TIMEOUT_S = 60.0
#: Values of :data:`PUSH_ENV` that mean "off".
_PUSH_FALSY = frozenset({"", "0", "false", "no", "off"})
#: Characters of git's complaint kept in a :class:`PushOutcome` reason.
_REASON_CAP = 500

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


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """What :func:`push_branch` did. ``pushed`` is the whole answer; ``reason`` says why not."""

    branch: str
    remote: str
    pushed: bool
    reason: str | None = None


def push_remote(env: Mapping[str, str] | None = None) -> str | None:
    """The remote a run should publish its branch to, or ``None`` when nobody asked for one.

    Reads :data:`PUSH_ENV`: off by default, ``1``/``true``/``yes``/``on`` means
    :data:`DEFAULT_REMOTE`, and any other value names the remote.
    """
    env = os.environ if env is None else env
    raw = (env.get(PUSH_ENV) or "").strip()
    if raw.lower() in _PUSH_FALSY:
        return None
    return DEFAULT_REMOTE if raw.lower() in {"1", "true", "yes", "on"} else raw


def push_branch(
    workdir: Path,
    branch: str,
    *,
    remote: str = DEFAULT_REMOTE,
    timeout: float | None = PUSH_TIMEOUT_S,
) -> PushOutcome:
    """Push ``branch`` from ``workdir`` to ``remote``, reporting instead of raising.

    This is a hook on a run that is already over, so it **fails soft**: no remote, no such
    branch, no git binary, a rejected push, a timeout — every one of them comes back as
    ``pushed=False`` with a ``reason``, and the caller turns that into a warning. It never
    raises and it never forces: a remote branch somebody else moved on is left alone, and the
    rejection is the reason.
    """
    try:
        if not branch_exists(workdir, branch):
            return PushOutcome(branch, remote, False, f"no local branch {branch!r} in {workdir}")
        if remote_url(workdir, remote) is None:
            return PushOutcome(branch, remote, False, f"no remote {remote!r} configured")
        result = run_git(
            ["push", "--set-upstream", remote, f"refs/heads/{branch}:refs/heads/{branch}"],
            workdir,
            check=False,
            timeout=timeout,
        )
    except (GitError, OSError) as exc:
        return PushOutcome(branch, remote, False, _reason(str(exc)))
    if result.ok:
        return PushOutcome(branch, remote, True)
    return PushOutcome(branch, remote, False, _reason(result.stderr or result.stdout))


def _reason(text: str) -> str:
    """One capped line of git's complaint (or a placeholder when it said nothing)."""
    line = " ".join(text.split())
    if not line:
        return "git push failed"
    return line[: _REASON_CAP - 1] + "…" if len(line) > _REASON_CAP else line


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
    "DEFAULT_REMOTE",
    "PUSH_ENV",
    "PUSH_TIMEOUT_S",
    "GitResult",
    "PushOutcome",
    "branch_exists",
    "common_dir",
    "current_branch",
    "fetch_prune",
    "git_executable",
    "is_ancestor",
    "is_dirty",
    "is_git_repo",
    "push_branch",
    "push_remote",
    "ref_exists",
    "remote_default_branch",
    "remote_url",
    "rev_parse",
    "run_git",
    "set_remote_head",
    "toplevel",
]
