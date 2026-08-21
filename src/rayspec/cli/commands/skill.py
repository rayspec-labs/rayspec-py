# SPDX-License-Identifier: Apache-2.0
"""`rayspec skill install|show|path` — the packaged Claude Code skill for coding agents.

Boundary: CLI presentation only; the data and the copy/compare helpers live in
:mod:`rayspec.skill`. ``install`` writes ``<project>/.claude/skills/rayspec/`` (or
``~/.claude/skills/rayspec/`` with ``--global``) with the same idempotence as ``rayspec init``;
``show`` compares the installed copies with the packaged skill; ``path`` prints the packaged dir.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from rayspec import __version__
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    console,
    err_console,
    fail,
    resolve_output,
)
from rayspec.cli.commands._skill_common import print_install_result, session_hint
from rayspec.loader import find_project_root
from rayspec.skill import (
    InstalledState,
    content_digest,
    global_skill_dir,
    install_skill,
    installed_state,
    project_skill_dir,
    skill_dir,
    skill_files,
)

RootOption = Annotated[
    Path | None,
    typer.Option(
        "--root",
        help="Project root (the directory that gets `.claude/skills/rayspec/`). Default: the "
        "nearest directory with `.rayspec/`, then `.git`, else the cwd. Not with --global.",
        show_default=False,
    ),
]


def resolve_root(root: Path | None) -> Path:
    """The project root an install targets: ``--root`` or :func:`find_project_root` from the cwd."""
    return (root or find_project_root(Path.cwd())).resolve()


def _state_line(label: str, state: InstalledState) -> str:
    if state.state == "missing":
        detail = "not installed"
    elif state.state == "current":
        detail = f"digest {state.digest} — up to date"
    else:
        detail = (
            f"digest {state.digest} — differs from the packaged skill "
            "(edited, or written by another rayspec version; "
            "rayspec skill install --force"
            + (" --global" if label == "global" else "")
            + " to refresh)"
        )
    return f"{label:<9} {escape(str(state.path))}  {escape(detail)}"


def _state_dict(state: InstalledState) -> dict[str, Any]:
    return {"path": str(state.path), "state": state.state, "digest": state.digest}


def register(app: typer.Typer) -> None:
    skill = typer.Typer(
        help="The rayspec skill for coding agents (Claude Code): install, show, path.",
        no_args_is_help=True,
    )
    app.add_typer(skill, name="skill")

    @skill.command("install")
    def install(
        global_: Annotated[
            bool,
            typer.Option(
                "--global",
                help="Install user-wide into `~/.claude/skills/rayspec/` instead of the project.",
            ),
        ] = False,
        force: Annotated[
            bool, typer.Option("--force", help="Overwrite files that already exist.")
        ] = False,
        root: RootOption = None,
    ) -> None:
        """Write the packaged skill to `<project>/.claude/skills/rayspec/` (or `--global`)."""
        if global_ and root is not None:
            fail(
                "--global and --root are mutually exclusive (a global install ignores the project)"
            )
        if global_:
            target = global_skill_dir()
            directory = target.parent.parent.parent
        else:
            directory = resolve_root(root)
            target = project_skill_dir(directory)
        try:
            results = install_skill(target, force=force)
        except OSError as exc:  # NotADirectoryError / IsADirectoryError / permissions …
            fail(f"cannot write the skill: {exc}")
            return  # unreachable: fail() raises typer.Exit
        print_install_result(results, target, label="global" if global_ else "project")
        created = sum(1 for r in results if r.action != "skipped")
        if not created:
            err_console().print(
                f"[yellow]warning:[/yellow] nothing written — all {len(results)} file(s) exist; "
                "use --force to overwrite them"
            )
        console().print(escape(session_hint(directory, global_install=global_)))

    @skill.command("show")
    def show(
        root: RootOption = None, json_: JsonOption = False, output: OutputOption = None
    ) -> None:
        """Show the packaged skill (version, digest, path) and the installed copies."""
        json_ = resolve_output(output, json_)
        project_root = resolve_root(root)
        packaged = skill_dir()
        digest = content_digest()
        project_state = installed_state(project_skill_dir(project_root))
        global_state = installed_state(global_skill_dir())
        if json_:
            typer.echo(
                json.dumps(
                    {
                        "packaged": {
                            "path": str(packaged),
                            "rayspec_version": __version__,
                            "digest": digest,
                            "files": [rel for rel, _ in skill_files()],
                        },
                        "project": _state_dict(project_state),
                        "global": _state_dict(global_state),
                    },
                    indent=2,
                )
            )
            return
        out = console()
        out.print(
            f"{'packaged':<9} {escape(str(packaged))}  "
            f"rayspec {escape(__version__)}, digest {digest}, {len(skill_files())} files"
        )
        out.print(_state_line("project", project_state))
        out.print(_state_line("global", global_state))

    @skill.command("path")
    def path() -> None:
        """Print the packaged skill directory (holds SKILL.md and references/)."""
        typer.echo(str(skill_dir()))
