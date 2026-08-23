# SPDX-License-Identifier: Apache-2.0
"""`rayspec skill install|show|path [NAME]` — the packaged Claude Code skills for coding agents.

Boundary: CLI presentation only; the registry and the copy/compare helpers live in
:mod:`rayspec.skill`. rayspec ships two skills (``rayspec-workflows``, ``rayspec-cli``); every
subcommand takes an optional NAME — no name means *all of them*, a name means that one, an
unknown name is exit 2 with a did-you-mean. ``install`` writes
``<project>/.claude/skills/<name>/`` (or ``~/.claude/skills/<name>/`` with ``--global``) with the
same idempotence as ``rayspec init``; ``show`` compares the installed copies with the packaged
skills; ``path`` prints the packaged dirs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.markup import escape

from rayspec import __version__
from rayspec.cli.commands._loader_common import (
    JsonOption,
    OutputOption,
    checked_root,
    console,
    err_console,
    fail,
    print_json,
    resolve_output,
)
from rayspec.cli.commands._skill_common import print_install_result, session_hint
from rayspec.loader import find_project_root
from rayspec.schema.base import suggest
from rayspec.skill import (
    SKILL_NAMES,
    SKILLS,
    InstalledState,
    Skill,
    content_digest,
    find_skill,
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
        help="Project root (the existing directory that gets `.claude/skills/`). "
        "Default: the nearest directory with `.rayspec/`, then `.git`, else the cwd. "
        "Not with --global.",
        show_default=False,
    ),
]

NameArgument = Annotated[
    str | None,
    typer.Argument(
        metavar="[NAME]",
        help="One skill (`rayspec-workflows` or `rayspec-cli`). Default: all of them.",
        show_default=False,
    ),
]


def resolve_root(root: Path | None) -> Path:
    """The project root an install targets: ``--root`` or :func:`find_project_root` from the cwd.

    ``--root`` goes through :func:`~rayspec.cli.commands._loader_common.checked_root` like
    everywhere else: a path that is not an existing directory is a usage error, not a directory
    tree this command creates on the way to reporting success.
    """
    return (checked_root(root) or find_project_root(Path.cwd())).resolve()


def resolve_skills(name: str | None) -> tuple[Skill, ...]:
    """The skills a subcommand acts on: every one, or the named one; unknown ⇒ exit 2."""
    if name is None:
        return SKILLS
    skill = find_skill(name)
    if skill is not None:
        return (skill,)
    hint = suggest(name, list(SKILL_NAMES))
    fail(
        f"unknown skill {name!r}",
        hint=(f"did you mean {hint!r}?" if hint else f"the skills are: {', '.join(SKILL_NAMES)}"),
    )
    raise AssertionError("unreachable")  # pragma: no cover


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


def _skill_report(skill: Skill, project_root: Path) -> dict[str, Any]:
    return {
        "name": skill.name,
        "packaged": {
            "path": str(skill_dir(skill)),
            "rayspec_version": __version__,
            "digest": content_digest(skill),
            "files": [rel for rel, _ in skill_files(skill)],
        },
        "project": _state_dict(installed_state(skill, project_skill_dir(skill, project_root))),
        "global": _state_dict(installed_state(skill, global_skill_dir(skill))),
    }


def register(app: typer.Typer) -> None:
    skill = typer.Typer(
        help="The rayspec skills for coding agents (Claude Code): install, show, path.",
        no_args_is_help=True,
    )
    app.add_typer(skill, name="skill")

    @skill.command("install")
    def install(
        name: NameArgument = None,
        global_: Annotated[
            bool,
            typer.Option(
                "--global",
                help="Install user-wide into `~/.claude/skills/` instead of the project.",
            ),
        ] = False,
        force: Annotated[
            bool, typer.Option("--force", help="Overwrite files that already exist.")
        ] = False,
        root: RootOption = None,
    ) -> None:
        """Write the packaged skills to `<project>/.claude/skills/` (or `--global`)."""
        if global_ and root is not None:
            fail(
                "--global and --root are mutually exclusive (a global install ignores the project)"
            )
        chosen = resolve_skills(name)
        if global_:
            directory = global_skill_dir(chosen[0]).parent.parent.parent
        else:
            directory = resolve_root(root)
        written = 0
        total = 0
        for one in chosen:
            target = global_skill_dir(one) if global_ else project_skill_dir(one, directory)
            try:
                results = install_skill(one, target, force=force)
            except OSError as exc:  # NotADirectoryError / IsADirectoryError / permissions …
                fail(f"cannot write the skill: {exc}")
                return  # unreachable: fail() raises typer.Exit
            print_install_result(results, target, label="global" if global_ else "project")
            written += sum(1 for r in results if r.action != "skipped")
            total += len(results)
        if not written:
            err_console().print(
                f"[yellow]warning:[/yellow] nothing written — all {total} file(s) exist; "
                "use --force to overwrite them"
            )
        console().print(escape(session_hint(directory, global_install=global_)))

    @skill.command("show")
    def show(
        name: NameArgument = None,
        root: RootOption = None,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """Show the packaged skills (version, digest, path) and the installed copies."""
        json_ = resolve_output(output, json_)
        project_root = resolve_root(root)
        chosen = resolve_skills(name)
        if json_:
            print_json({"skills": [_skill_report(one, project_root) for one in chosen]})
            return
        out = console()
        for index, one in enumerate(chosen):
            if index:
                out.print()
            out.print(f"[bold]{escape(one.name)}[/bold] — {escape(one.summary)}")
            out.print(
                f"{'packaged':<9} {escape(str(skill_dir(one)))}  "
                f"rayspec {escape(__version__)}, digest {content_digest(one)}, "
                f"{len(skill_files(one))} files"
            )
            out.print(
                _state_line("project", installed_state(one, project_skill_dir(one, project_root)))
            )
            out.print(_state_line("global", installed_state(one, global_skill_dir(one))))

    @skill.command("path")
    def path(name: NameArgument = None) -> None:
        """Print the packaged skill directories (each holds SKILL.md and references/)."""
        for one in resolve_skills(name):
            typer.echo(str(skill_dir(one)))
