# SPDX-License-Identifier: Apache-2.0
"""The worktree change guard: how much of the repository an agent was allowed to rewrite.

Boundary: measurement and comparison only. This module shells out to ``git`` through
:mod:`rayspec.workspace.git`, compares the result against limits it is handed, and reports what
exceeded which limit. It reads no policy file, fails no step and writes nothing — the caller
decides what a violation means. The limits themselves come from ``policy.yaml``'s ``workspace:``
block (:mod:`rayspec.policy`).

The measurement is always against the run's ``base_sha`` and covers the whole repository, not the
sub-directory a step happened to work in: an agent that rewrites half the repo does it wherever
it likes. Untracked files count as additions, because "quietly wrote 400 new files" is exactly
the case a diff against HEAD would miss — including files ``.gitignore`` hides, since ``.env``,
a gitignored ``secrets/`` and a build directory are the paths most worth protecting. Renames
count on both sides, so moving a file out of a protected directory is still a change to it.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from rayspec.workspace.errors import GitError
from rayspec.workspace.git import run_git, toplevel

#: How many changed files a report lists before it says "and N more".
SUMMARY_LIMIT = 8

#: Bytes of an untracked file inspected before deciding it is binary.
_SNIFF = 8192

ViolationKind = Literal["protected_path", "max_changed_files", "max_changed_lines"]


@dataclass(frozen=True, slots=True)
class ChangedFile:
    """One file that differs from the base commit (or is new and untracked)."""

    path: str
    added: int = 0
    deleted: int = 0
    binary: bool = False
    untracked: bool = False

    @property
    def lines(self) -> int:
        """Added plus deleted lines (``0`` for a binary file — git counts no lines either)."""
        return self.added + self.deleted

    def render(self) -> str:
        """``src/app.py (+12/-3)`` — or ``(binary)`` when git reported no line counts."""
        detail = "binary" if self.binary else f"+{self.added}/-{self.deleted}"
        return f"{self.path} ({detail})"


@dataclass(frozen=True, slots=True)
class ChangeSummary:
    """What changed in a worktree since ``base_sha``."""

    base_sha: str
    files: tuple[ChangedFile, ...] = ()

    @property
    def changed_files(self) -> int:
        return len(self.files)

    @property
    def changed_lines(self) -> int:
        return sum(f.lines for f in self.files)

    def render(self, limit: int = SUMMARY_LIMIT) -> str:
        """``a.py (+1/-0), b.py (+2/-1), … (+3 more)`` — the diff summary shown on a violation."""
        if not self.files:
            return "no changes"
        shown = [f.render() for f in self.files[:limit]]
        rest = len(self.files) - len(shown)
        if rest > 0:
            shown.append(f"… (+{rest} more)")
        return ", ".join(shown)


@dataclass(frozen=True, slots=True)
class GuardViolation:
    """One limit the changes broke, already phrased for a person reading a failed step."""

    kind: ViolationKind
    message: str


@dataclass(frozen=True, slots=True)
class ChangeGuardReport:
    """The outcome of one guard check: what changed, and which limits that broke."""

    summary: ChangeSummary
    violations: tuple[GuardViolation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def message(self) -> str:
        """Multi-line report: every broken limit, then the diff summary."""
        if self.ok:
            return f"change guard: within limits ({self.summary.render()})"
        base = self.summary.base_sha[:12]
        head = f"change guard: {len(self.violations)} limit(s) exceeded since {base}"
        body = [f"  - {v.message}" for v in self.violations]
        return "\n".join([head, *body, f"  changed: {self.summary.render()}"])


def match_path(path: str, pattern: str) -> bool:
    """Whether the repo-relative POSIX ``path`` is covered by a protected-path ``pattern``.

    Globs are matched the way a person expects from a ``.gitignore``-ish pattern: ``*`` crosses
    directory separators, a pattern ending in ``/`` covers the whole directory, a leading ``**/``
    is optional, and a pattern with no separator also matches a file's bare name in any directory.
    """
    pattern = pattern.strip()
    if not pattern:
        return False
    if pattern.endswith("/"):
        pattern += "**"
    if fnmatch.fnmatchcase(path, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]):
        return True
    return "/" not in pattern and fnmatch.fnmatchcase(PurePosixPath(path).name, pattern)


def _top(workdir: Path) -> Path:
    top = toplevel(Path(workdir))
    if top is None:
        raise GitError(
            f"{workdir} is not inside a git work tree",
            hint="the change guard measures a worktree against its base commit",
        )
    return top


def _tracked_changes(top: Path, base_sha: str) -> list[ChangedFile]:
    """Parse ``git diff --numstat -z`` as a token stream rather than record by record.

    ``-z`` writes a rename or a copy as THREE NUL-separated records — ``"<add>\t<del>\t"``, then
    the old path, then the new one. Splitting on NUL and reading the third TAB field therefore
    yields an empty path and silently drops both real ones; rename detection is on by default, so
    ``git mv`` of a protected file would slip past the guard. Both sides of a rename are reported,
    because moving a file *out* of a protected directory changes the protected path too; the line
    counts go to the destination so a rename is not counted twice.
    """
    result = run_git(["diff", "--numstat", "-z", base_sha, "--"], top)
    records = result.stdout.split("\0")
    out: list[ChangedFile] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record.strip():
            continue
        parts = record.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        binary = added == "-" or deleted == "-"
        counted = ChangedFile(
            path=path,
            added=0 if binary else int(added or 0),
            deleted=0 if binary else int(deleted or 0),
            binary=binary,
        )
        if path:
            out.append(counted)
            continue
        sides = [name for name in records[index : index + 2] if name]
        index += 2
        for position, name in enumerate(sides):
            # the destination carries the line counts, the source is a touched path with none:
            # one line delta, two paths a protected glob can match.
            destination = position == len(sides) - 1
            out.append(
                replace(counted, path=name)
                if destination
                else ChangedFile(path=name, binary=binary)
            )
    return out


def _count_lines(path: Path) -> tuple[int, bool]:
    """``(lines, binary)`` of an untracked file; unreadable files count as zero lines."""
    try:
        with path.open("rb") as handle:
            head = handle.read(_SNIFF)
            if b"\0" in head:
                return 0, True
            lines = head.count(b"\n")
            tail = head
            while chunk := handle.read(1 << 20):
                lines += chunk.count(b"\n")
                tail = chunk
            if tail and not tail.endswith(b"\n"):
                lines += 1  # a last line without a trailing newline is still a line
            return lines, False
    except OSError:
        return 0, False


def _untracked_changes(top: Path, *, include_ignored: bool) -> list[ChangedFile]:
    """Untracked files, by default including the ones ``.gitignore`` hides.

    ``--exclude-standard`` would hide exactly the paths worth protecting — ``.env``, a gitignored
    ``secrets/``, a build directory — so the guard does not pass it unless asked to. A repository
    with a large ignored tree (``node_modules``, a virtualenv) is why the switch exists.
    """
    args = ["ls-files", "--others", "--full-name", "-z"]
    if not include_ignored:
        args.insert(2, "--exclude-standard")
    result = run_git(args, top)
    out: list[ChangedFile] = []
    for rel in result.stdout.split("\0"):
        if not rel:
            continue
        lines, binary = _count_lines(top / rel)
        out.append(ChangedFile(path=rel, added=lines, binary=binary, untracked=True))
    return out


def diff_since(
    workdir: Path,
    base_sha: str,
    *,
    include_untracked: bool = True,
    include_ignored: bool = True,
) -> ChangeSummary:
    """Measure the worktree containing ``workdir`` against ``base_sha``.

    ``include_ignored`` keeps files that ``.gitignore`` hides in the measurement, which is the
    default: an agent writing into a gitignored directory is exactly the case a guard is for.
    Pass ``False`` in a repository whose ignored tree is large enough to make the walk pointless.

    Raises :class:`~rayspec.workspace.errors.GitError` when ``workdir`` is not in a work tree or
    ``base_sha`` is not a commit — a guard that cannot measure must never report "all clear".
    """
    top = _top(Path(workdir))
    files = _tracked_changes(top, base_sha)
    if include_untracked:
        files.extend(_untracked_changes(top, include_ignored=include_ignored))
    files.sort(key=lambda f: f.path)
    return ChangeSummary(base_sha=base_sha, files=tuple(files))


def check_change_guard(
    workdir: Path,
    base_sha: str,
    *,
    protected_paths: Sequence[str] | Iterable[str] = (),
    max_changed_files: int | None = None,
    max_changed_lines: int | None = None,
    include_untracked: bool = True,
    include_ignored: bool = True,
) -> ChangeGuardReport:
    """Measure the worktree and compare it with the guard's limits.

    Every broken limit is reported, not just the first: a step that both touched a protected path
    and rewrote too much should say so once, rather than one problem per re-run.
    """
    summary = diff_since(
        workdir,
        base_sha,
        include_untracked=include_untracked,
        include_ignored=include_ignored,
    )
    violations: list[GuardViolation] = []
    for pattern in protected_paths:
        hits = [f.path for f in summary.files if match_path(f.path, pattern)]
        if hits:
            shown = ", ".join(hits[:SUMMARY_LIMIT])
            rest = len(hits) - min(len(hits), SUMMARY_LIMIT)
            more = f" (+{rest} more)" if rest else ""
            violations.append(
                GuardViolation(
                    "protected_path",
                    f"protected_path: {pattern!r} matched {shown}{more}",
                )
            )
    if max_changed_files is not None and summary.changed_files > max_changed_files:
        violations.append(
            GuardViolation(
                "max_changed_files",
                f"max_changed_files: {summary.changed_files} files changed, "
                f"limit {max_changed_files}",
            )
        )
    if max_changed_lines is not None and summary.changed_lines > max_changed_lines:
        violations.append(
            GuardViolation(
                "max_changed_lines",
                f"max_changed_lines: {summary.changed_lines} lines changed, "
                f"limit {max_changed_lines}",
            )
        )
    return ChangeGuardReport(summary=summary, violations=tuple(violations))


__all__ = [
    "SUMMARY_LIMIT",
    "ChangeGuardReport",
    "ChangeSummary",
    "ChangedFile",
    "GuardViolation",
    "ViolationKind",
    "check_change_guard",
    "diff_since",
    "match_path",
]
