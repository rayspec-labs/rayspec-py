# SPDX-License-Identifier: Apache-2.0
"""Approval prompt seam: the protocol the ``approve:`` executor calls on a TTY, plus the
Rich-based implementation the CLI injects (tests inject fakes).

The console prompt shows the plan's panel (message, per-need status/duration/cost + output
tail, ``git status --short`` / ``git diff --stat`` of the workdir, run totals) and the keys
``[a]pprove [r]eject [v]iew [d]iff [p]ause``: ``v`` prints the full output of the ``needs``,
``d`` prints ``git diff`` of the workdir (both best effort, via ``anyio.to_thread``).

Module boundary: no engine state here — the executor (``executors/approve.py``) owns quiesce,
pause tokens and decision recording; this module only asks a human.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from anyio import to_thread

from rayspec.providers.base import Usage
from rayspec.providers.pricing import format_cost, format_tokens
from rayspec.textsafe import safe_text

#: Lines kept from ``git status --short`` / ``git diff --stat`` in the panel, and from ``[d]iff``.
_SUMMARY_LINES = 30
_DIFF_LINES = 400
_GIT_TIMEOUT_S = 5.0

#: ANSI escape sequences — CSI ``ESC [ … final``, OSC ``ESC ] … (BEL | ESC \\)`` (a pasted
#: terminal title / hyperlink), ``ESC O x`` SS3 keys, ``ESC <printable>`` (Meta/Alt-x without
#: readline, other Fe sequences) — and C0/DEL control characters except TAB: what arrow/function/
#: modifier keys leave in a plain ``input()``.
_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1bO[@-~]"
    r"|\x1b[ -~]"
    r"|[\x00-\x08\x0a-\x1f\x7f]"
)


def clean_answer(raw: str) -> str:
    """Strip escape sequences / control characters from a typed answer.

    Without ``readline`` an arrow key at ``input()`` lands as ``ESC [ C`` in the string and
    Alt-a as ``ESC a`` (dropped whole: a modifier keystroke must never approve a gate); with
    readline the line editor handles the keys. Either way the answer rayspec interprets is the
    visible text only (TAB and printable Unicode are kept).
    """
    return _ESCAPE_RE.sub("", raw)


def enable_readline() -> None:
    """Best effort ``import readline`` so ``input()`` gets line editing (arrow keys, history).

    Only on a real terminal (stdin **and** stdout TTYs): GNU readline may print a terminal
    initialisation sequence on import, which must never land in piped output.
    """
    if "readline" in sys.modules or not _real_tty():
        return
    with contextlib.suppress(ImportError, OSError):
        import readline  # noqa: F401 - importing it is the side effect


def _real_tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def humanize_duration(ms: float | None) -> str:
    """``0.3s`` · ``1.2s`` · ``31m 52s`` · ``1h 3m``; ``—`` when unknown."""
    if ms is None:
        return "—"
    seconds = max(0.0, float(ms)) / 1000.0
    if seconds < 59.95:  # below the point where ``.1f`` would print ``60.0s``
        return f"{seconds:.1f}s"
    minutes, sec = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def fmt_cost(cost_usd: float | None, cost_source: str = "provider") -> str:
    """``$0.50`` (provider), ``~$0.50`` (pricing-table estimate), ``≥$0.50`` (partial: some
    steps have tokens but no price), ``—`` when no cost is known.

    Thin wrapper over :func:`rayspec.providers.pricing.format_cost`; tokens never show up in a
    cost slot (the panel has its own tokens figure).
    """
    if cost_usd is None:
        return "—"
    return format_cost(cost_usd, cost_source, Usage())


@dataclass(frozen=True, slots=True)
class ApprovalNeed:
    """Summary of one upstream step shown in the approval panel."""

    path: str
    status: str
    duration_ms: int | None = None
    cost_usd: float | None = None
    tail: str = ""
    #: the full output text (capped by the executor) shown by ``[v]iew``
    output: str = ""
    #: ``provider`` | ``table`` (estimate, rendered ``~$``) | ``none`` — additive
    cost_source: str = "none"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """What the approver sees."""

    run_id: str
    step_path: str
    message: str
    attempt: int
    workdir: str
    needs: Sequence[ApprovalNeed] = ()
    totals: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalAnswer:
    """A decision. ``approved=False`` rejects; the comment becomes the step output."""

    approved: bool
    comment: str = ""


@runtime_checkable
class ApprovalPrompt(Protocol):
    """Ask a human. Return ``None`` to *pause* the run instead (exit 3)."""

    async def __call__(self, request: ApprovalRequest) -> ApprovalAnswer | None: ...


def _run_git(workdir: str, args: Sequence[str], *, timeout_s: float = _GIT_TIMEOUT_S) -> str | None:
    """stdout of ``git <args>`` in ``workdir``; ``None`` when git is missing/fails/times out."""
    try:
        proc = subprocess.run(  # fixed argv, no shell
            ["git", *args],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _cap(text: str, max_lines: int) -> str:
    lines = text.rstrip("\n").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + f"\n… ({len(lines) - max_lines} more lines)"


def git_summary(workdir: str) -> str:
    """``git status --short`` + ``git diff --stat`` of ``workdir`` (``''`` when not a repo)."""
    parts: list[str] = []
    for label, args in (
        ("git status --short", ("status", "--short")),
        ("git diff --stat", ("diff", "--stat")),
    ):
        text = _run_git(workdir, args)
        if text is None:
            return ""
        if text.strip():
            parts.append(f"$ {label}\n{_cap(text, _SUMMARY_LINES)}")
    return "\n".join(parts)


def git_diff(workdir: str) -> str:
    """``git diff HEAD`` (else ``git diff``) of ``workdir``, capped; a note when unavailable."""
    text = _run_git(workdir, ("diff", "HEAD"))
    if text is None:
        text = _run_git(workdir, ("diff",))
    if text is None:
        return "(git diff unavailable: not a git repository or git is not installed)"
    if not text.strip():
        return "(no changes in the workdir)"
    return _cap(text, _DIFF_LINES)


def format_totals(totals: dict[str, Any]) -> str:
    """``steps: 3 · tokens: 12.3k tok · cost: —`` — the run totals line of the panel.

    Known keys are formatted (``tokens`` via :func:`format_tokens`, ``cost_usd`` via
    :func:`fmt_cost` with an optional ``cost_source`` key, ``duration_ms`` humanized); unknown
    keys print as ``key: value`` with ``None`` rendered as ``—``.
    """
    parts: list[str] = []
    source = str(totals.get("cost_source") or "provider")
    for key, value in totals.items():
        if key == "cost_source":
            continue
        if key == "cost_usd":
            parts.append(f"cost: {fmt_cost(value, source)}")
        elif key == "tokens":
            parts.append(f"tokens: {format_tokens(int(value or 0))}")
        elif key == "duration_ms":
            parts.append(f"duration: {humanize_duration(value)}")
        else:
            parts.append(f"{key}: {'—' if value is None else value}")
    return " · ".join(parts)


class ConsoleApprovalPrompt:
    """Rich panel + ``[a]pprove [r]eject [v]iew [d]iff [p]ause`` keys on the terminal (rich
    imported lazily). ``git`` information is gathered in a worker thread, best effort."""

    def __init__(self, console: Any | None = None) -> None:
        self._console = console

    def _get_console(self) -> Any:
        if self._console is None:
            from rich.console import Console

            self._console = Console(stderr=True)
        return self._console

    def render(self, request: ApprovalRequest, git_info: str = "") -> None:
        """Print the approval panel (``git_info`` = :func:`git_summary` of the workdir)."""
        from rich.panel import Panel
        from rich.text import Text

        console = self._get_console()
        # everything below comes from the workflow, the agents or the workdir: untrusted text,
        # rendered as plain ``Text`` with escape sequences removed
        body = Text()
        body.append(safe_text(request.message).strip() + "\n", style="bold")
        if request.needs:
            body.append("\nupstream:\n", style="dim")
            for need in request.needs:
                line = f"  {safe_text(need.path)}  {safe_text(need.status)}"
                if need.duration_ms is not None:
                    line += f"  {humanize_duration(need.duration_ms)}"
                if need.cost_usd is not None:
                    line += f"  {fmt_cost(need.cost_usd, need.cost_source)}"
                body.append(line + "\n")
                if need.tail:
                    for raw in safe_text(need.tail).splitlines()[-15:]:
                        body.append(f"    {raw}\n", style="dim")
        if git_info:
            body.append(f"\nworkdir {safe_text(request.workdir)}:\n", style="dim")
            for raw in safe_text(git_info).splitlines():
                body.append(f"  {raw}\n", style="dim")
        if request.totals:
            body.append("\n" + format_totals(request.totals), style="dim")
        title = Text(
            f"Approval required — {safe_text(request.step_path)} (attempt {request.attempt})"
        )
        console.print(Panel(body, title=title, border_style="yellow"))

    def view(self, request: ApprovalRequest) -> None:
        """``[v]iew``: print the full output of every ``needs`` step (plain text)."""
        from rich.panel import Panel
        from rich.text import Text

        console = self._get_console()
        if not request.needs:
            console.print("[dim](this gate has no needs to view)[/dim]")
            return
        for need in request.needs:
            text = need.output or need.tail or "(no output)"
            console.print(
                Panel(
                    Text(safe_text(text)),
                    title=Text(f"{safe_text(need.path)} ({safe_text(need.status)})"),
                    border_style="dim",
                )
            )

    async def _input(self, prompt: str) -> str:
        """Ask on a worker thread, with escape sequences stripped.

        On a real terminal with readline loaded the builtin ``input(prompt)`` is used so the
        line editor owns the prompt (correct redisplay after Ctrl-U / wrapping); otherwise
        ``console.input`` (tests script it).
        """
        console = self._get_console()

        def ask() -> str:
            if "readline" in sys.modules and _real_tty():
                return input(prompt)
            return console.input(prompt, markup=False)

        raw = await to_thread.run_sync(ask, abandon_on_cancel=True)
        return clean_answer(raw)

    async def __call__(self, request: ApprovalRequest) -> ApprovalAnswer | None:
        console = self._get_console()
        enable_readline()
        git_info = await to_thread.run_sync(git_summary, request.workdir)
        self.render(request, git_info)
        while True:
            try:
                raw = await self._input("[a]pprove / [r]eject / [v]iew / [d]iff / [p]ause > ")
            except (EOFError, KeyboardInterrupt):
                return None
            key = raw.strip().lower()[:1]
            if key == "a":
                comment = await self._input("comment (optional) > ")
                return ApprovalAnswer(True, comment.strip())
            if key == "r":
                comment = await self._input("reason (optional) > ")
                return ApprovalAnswer(False, comment.strip())
            if key == "p":
                return None
            if key == "v":
                self.view(request)
                continue
            if key == "d":
                diff = await to_thread.run_sync(git_diff, request.workdir)
                console.print(safe_text(diff), markup=False, highlight=False, soft_wrap=True)
                continue
            console.print("[dim]please answer a, r, v, d or p[/dim]")


__all__ = [
    "ApprovalAnswer",
    "ApprovalNeed",
    "ApprovalPrompt",
    "ApprovalRequest",
    "ConsoleApprovalPrompt",
    "clean_answer",
    "enable_readline",
    "fmt_cost",
    "format_totals",
    "git_diff",
    "git_summary",
    "humanize_duration",
]
