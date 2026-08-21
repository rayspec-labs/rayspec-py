# SPDX-License-Identifier: Apache-2.0
"""`rayspec completion <shell>` — print an opt-in shell-completion script, and serve its values.

Boundary: this module owns everything about shell completion. The Typer app is built with
``add_completion=False`` and stays that way: that option pair is ``--install-completion``, which
**appends a source line to the user's ``~/.bashrc`` / ``~/.zshrc``** as a side effect of a flag,
and ``--show-completion``, which sniffs the shell through ``shellingham``. Both would also sit in
the root ``--help`` of every command. An explicit ``rayspec completion <shell>`` prints the same
script and lets the user decide where it goes.

``add_completion=False`` also leaves Click's shell registry empty, so the completion protocol
answers ``Shell bash not supported``. :func:`enable_shell_completion` fills it in — only while a
completion request is in flight, so an ordinary invocation never mutates that global state.

The emitted script is Typer's own script plus one wrapper: the argument slots that are worth
completing — a workflow name, a run id — are values only rayspec can produce, so the wrapper asks
for them with ``rayspec completion --values <kind>`` and otherwise defers to Typer for commands
and options.
"""

from __future__ import annotations

import contextlib
import io
import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from rayspec.cli.commands._loader_common import RootOption, console, fail, make_context

#: The environment variable the completion scripts set; Click dispatches on it.
COMPLETE_VAR = "_RAYSPEC_COMPLETE"

#: The program name the emitted script binds to (the console script of the wheel).
PROG_NAME = "rayspec"

#: Shells the emitted wrapper supports.
SHELLS: tuple[str, ...] = ("bash", "zsh", "fish")

#: Commands whose next argument is a workflow name.
WORKFLOW_COMMANDS: tuple[str, ...] = ("run", "plan", "validate", "test")

#: Commands whose next argument is a run id (``runs diff`` / ``runs stubs`` included).
RUN_COMMANDS: tuple[str, ...] = (
    "show",
    "logs",
    "resume",
    "approve",
    "reject",
    "cancel",
    "eval",
    "explain",
    "diff",
    "stubs",
)

#: How many run ids a completion callback offers (newest first).
RUN_LIMIT = 50


class Shell(StrEnum):
    """The ``SHELL`` argument of ``rayspec completion``."""

    bash = "bash"
    zsh = "zsh"
    fish = "fish"


class ValueKind(StrEnum):
    """The ``--values`` kinds the emitted script calls back for."""

    workflows = "workflows"
    runs = "runs"


def enable_shell_completion() -> bool:
    """Register Typer's shell-completion classes for an in-flight completion request.

    Typer only calls ``completion_init()`` for an app built with ``add_completion=True``; without
    it Click's shell registry is empty and the ``_RAYSPEC_COMPLETE`` protocol answers ``Shell bash
    not supported``. Calling it here — and only when the variable is set — keeps completion
    working while leaving that registry untouched for every ordinary invocation. ``False`` when
    the private module is not importable, in which case completion degrades to nothing rather
    than crashing the CLI.
    """
    try:
        from typer._completion_classes import completion_init
    except ImportError:  # pragma: no cover - a Typer build without the private module
        return False
    completion_init()
    return True


def workflow_values(root: Path | None = None) -> list[str]:
    """Workflow names for completion, or ``[]`` when anything at all goes wrong.

    A completion callback runs inside the user's shell: an error message here would land in the
    candidate list, so every failure (no project, unreadable config, a half-written workflow) is
    silence and the shell falls back to its own file completion.
    """
    try:
        from rayspec.loader import discover_workflows

        with contextlib.redirect_stderr(io.StringIO()):
            ctx = make_context(root, project_env=False)
            return [ref.name for ref in discover_workflows(ctx.project_root, home=ctx.home)]
    except Exception:  # a completion callback never speaks up — see the docstring
        return []


def run_values(root: Path | None = None, *, limit: int = RUN_LIMIT) -> list[str]:
    """The project's most recent run ids (newest first), or ``[]`` — see :func:`workflow_values`."""
    try:
        from rayspec.cli._runs_common import make_runs_context

        with contextlib.redirect_stderr(io.StringIO()):
            return make_runs_context(root).store.list_run_ids()[:limit]
    except Exception:  # see workflow_values
        return []


def _typer_script(shell: str) -> str:
    """Typer's own completion script for ``shell`` (commands and options)."""
    from typer._completion_shared import get_completion_script

    return get_completion_script(prog_name=PROG_NAME, complete_var=COMPLETE_VAR, shell=shell)


def _case_arms(indent: str) -> str:
    workflows = "|".join(WORKFLOW_COMMANDS)
    runs = "|".join(RUN_COMMANDS)
    return (
        f'{indent}{workflows}) values="$({PROG_NAME} completion --values workflows '
        f'2>/dev/null)" ;;\n'
        f'{indent}{runs}) values="$({PROG_NAME} completion --values runs 2>/dev/null)" ;;'
    )


def _bash_script() -> str:
    return f"""{_typer_script("bash")}

# rayspec: complete the argument slots Typer cannot know about (workflow names, run ids) and
# fall back to the completion above for everything else.
_{PROG_NAME}_values_completion() {{
    local cur prev values
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    values=""
    case "$prev" in
{_case_arms(" " * 8)}
    esac
    if [ -n "$values" ]; then
        COMPREPLY=( $(compgen -W "$values" -- "$cur") )
        return 0
    fi
    _{PROG_NAME}_completion "$@"
}}

complete -o default -F _{PROG_NAME}_values_completion {PROG_NAME}
"""


def _zsh_script() -> str:
    return f"""{_typer_script("zsh")}

# rayspec: complete the argument slots Typer cannot know about (workflow names, run ids) and
# fall back to the completion above for everything else.
_{PROG_NAME}_values_completion() {{
  local prev values
  prev="${{words[CURRENT-1]}}"
  values=""
  case "$prev" in
{_case_arms(" " * 4)}
  esac
  if [[ -n "$values" ]]; then
    compadd -- ${{(f)values}}
    return
  fi
  _{PROG_NAME}_completion
}}

compdef _{PROG_NAME}_values_completion {PROG_NAME}
"""


def _fish_script() -> str:
    workflows = " ".join(WORKFLOW_COMMANDS)
    runs = " ".join(RUN_COMMANDS)
    return f"""{_typer_script("fish")}

# rayspec: complete the argument slots Typer cannot know about (workflow names, run ids).
complete --command {PROG_NAME} --no-files \
--arguments "({PROG_NAME} completion --values workflows)" \
--condition "__fish_seen_subcommand_from {workflows}"
complete --command {PROG_NAME} --no-files \
--arguments "({PROG_NAME} completion --values runs)" \
--condition "__fish_seen_subcommand_from {runs}"
"""


#: Script builders, one per supported shell.
SCRIPTS = {"bash": _bash_script, "zsh": _zsh_script, "fish": _fish_script}


def completion_script(shell: str) -> str:
    """The completion script for ``shell`` — Typer's own plus the workflow/run-id wrapper."""
    if shell not in SCRIPTS:
        raise LookupError(shell)
    return SCRIPTS[shell]()


def register(app: typer.Typer) -> None:
    if os.environ.get(COMPLETE_VAR):
        enable_shell_completion()

    @app.command()
    def completion(
        shell: Annotated[
            Shell | None,
            typer.Argument(
                help=f"Which shell to print a script for: {', '.join(SHELLS)}.",
                show_default=False,
            ),
        ] = None,
        values: Annotated[
            ValueKind | None,
            typer.Option(
                "--values",
                help="Print completion candidates instead of a script (what the script calls).",
                show_default=False,
            ),
        ] = None,
        root: RootOption = None,
    ) -> None:
        """Print a shell-completion script to source (`rayspec completion zsh >> ~/.zshrc`)."""
        if values is not None:
            if shell is not None:
                fail(f"--values does not take a shell (drop `{shell.value}`)")
            out = console()
            found = workflow_values(root) if values is ValueKind.workflows else run_values(root)
            for value in found:
                out.print(value, markup=False, highlight=False)
            return
        if shell is None:
            fail(
                f"which shell? one of: {', '.join(SHELLS)}",
                hint=f"rayspec completion {SHELLS[0]}",
            )
            return  # unreachable: fail() raises typer.Exit
        console().print(completion_script(shell.value), markup=False, highlight=False)
