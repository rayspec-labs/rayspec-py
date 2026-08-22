# SPDX-License-Identifier: Apache-2.0
"""Presentation helpers shared by `rayspec skill install` and `rayspec init`.

Boundary: pure output formatting for :func:`rayspec.skill.install_skill` results — no filesystem
access, no business logic. Lives next to ``_loader_common`` so the two command modules stay
independent plug-ins (``app.py`` discovers every non-underscore module of this package).
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape

from rayspec.cli.commands._loader_common import console
from rayspec.skill import InstalledFile


def print_install_result(results: list[InstalledFile], target: Path, *, label: str) -> None:
    """Print one line per file (``created`` / ``overwrote`` / ``exists … skipped``) + a summary."""
    out = console()
    shown_root = target.parent.parent.parent  # <root>/.claude/skills/<name> -> <root>
    for item in results:
        try:
            rel = item.path.relative_to(shown_root).as_posix()
        except ValueError:
            rel = str(item.path)
        if item.action == "skipped":
            out.print(
                f"[yellow]exists [/yellow]  {escape(rel)} "
                "[dim](skipped; use --force to overwrite)[/dim]"
            )
        else:
            verb = "created" if item.action == "created" else "overwrote"
            out.print(f"[green]{verb}[/green]  {escape(rel)}")
    created = sum(1 for r in results if r.action != "skipped")
    skipped = len(results) - created
    summary = f"{created} file(s) written"
    if skipped:
        summary += f", {skipped} kept"
    out.print(f"[bold]{label}[/bold] skill in {escape(str(target))}: {summary}")


def session_hint(directory: Path, *, global_install: bool) -> str:
    """The one-line "open a fresh session" hint printed after an install."""
    where = "any directory" if global_install else str(directory)
    return (
        f"open a fresh Claude Code session in {where} — the rayspec skills load automatically "
        "(rayspec skill show)"
    )


__all__ = ["print_install_result", "session_hint"]
