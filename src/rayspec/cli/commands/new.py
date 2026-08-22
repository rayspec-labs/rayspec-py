# SPDX-License-Identifier: Apache-2.0
"""`rayspec new workflow|agent <name>` — add one file to a project that already exists.

Boundary: renders a packaged template (:mod:`rayspec.cli.templates`, the ``new/`` directory) into
``.rayspec/workflows/`` or ``.rayspec/agents/`` and prints what to run next. It never creates a
project — that is ``rayspec init`` — and it holds no loader or engine logic; the rendered files
are validated by tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml
from rich.markup import escape

from rayspec.cli.commands._loader_common import RootOption, checked_root, console, fail
from rayspec.loader import discover_agents, find_project_root
from rayspec.schema.base import suggest
from rayspec.schema.common import validate_identifier, validate_name

#: Where a project keeps its documents, relative to the project root.
PROJECT_DIR = ".rayspec"

#: The template directory these commands render from.
TEMPLATE_DIR = "new"

#: What ``rayspec new <kind>`` writes, per kind: the sub-directory and the template file.
KINDS: dict[str, tuple[str, str]] = {
    "workflow": ("workflows", "workflow.yaml"),
    "agent": ("agents", "agent.yaml"),
}

#: Fallback description of a fresh workflow (``--description`` overrides it).
DEFAULT_DESCRIPTION = "One agent step. Replace this description and the prompt below."

NewAction = Literal["created", "overwritten"]


@dataclass(frozen=True, slots=True)
class NewFile:
    """One rendered file: its path relative to the project root, and what happened."""

    relative: str
    path: Path
    action: NewAction


def _template(name: str) -> str:
    return (resources.files("rayspec.cli.templates") / TEMPLATE_DIR / name).read_text(
        encoding="utf-8"
    )


def yaml_scalar(text: str) -> str:
    """``text`` as a single-line YAML scalar, quoted only when it has to be.

    A description arrives from the shell and may hold ``:``, ``#`` or quotes; substituting it raw
    would produce a document that no longer parses. Newlines are collapsed so the value always
    fits the one-line ``description:`` of the template.
    """
    collapsed = " ".join(text.split())
    dumped = yaml.safe_dump(
        {"description": collapsed}, default_flow_style=False, allow_unicode=True, width=10**6
    )
    return dumped.split(":", 1)[1].strip()


def workflow_text(name: str, *, agent: str | None = None, description: str = "") -> str:
    """The rendered workflow document for ``rayspec new workflow <name>``.

    With ``agent`` the template that references a named agent file is used (no inline ``agents:``
    block); without it the workflow carries one inline agent named ``assistant``.
    """
    template = _template("workflow_agent.yaml" if agent else "workflow.yaml")
    return (
        template.replace("__NAME__", name)
        .replace("__AGENT__", agent or "assistant")
        .replace("__DESCRIPTION__", yaml_scalar(description or DEFAULT_DESCRIPTION))
    )


def agent_text(name: str) -> str:
    """The rendered agent document for ``rayspec new agent <name>``."""
    return _template("agent.yaml").replace("__NAME__", name)


def write_new(root: Path, kind: str, name: str, text: str, *, force: bool = False) -> NewFile:
    """Write ``text`` to ``<root>/.rayspec/<dir>/<name>.yaml``; refuse an existing file.

    Raises :class:`FileExistsError` when the file is there and ``force`` is false,
    :class:`IsADirectoryError` when a directory sits at the target path, and any other
    :class:`OSError` unchanged — the CLI maps them to ``error: …`` + exit 2.
    """
    subdir, _ = KINDS[kind]
    relative = f"{PROJECT_DIR}/{subdir}/{name}.yaml"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        raise IsADirectoryError(f"{target} is a directory, expected a file (or nothing)")
    existed = target.exists()
    if existed and not force:
        raise FileExistsError(f"{relative} already exists")
    target.write_text(text, encoding="utf-8")
    return NewFile(relative, target, "overwritten" if existed else "created")


def project_root_for(root: Path | None) -> Path:
    """The project to add to: ``--root`` itself, else the walk-up other project commands do.

    An explicit ``--root`` *names* the project and is taken as given. ``find_project_root`` walks
    **up** from where it starts, so starting it at ``--root`` would add the file to an enclosing
    project when the named directory has no ``.rayspec/`` — a typo'd path would land somewhere
    the user never named and be reported as a path relative to it. Without ``--root`` the walk-up
    is exactly what the other project commands do, so ``new`` works from a sub-directory.

    A directory without ``.rayspec/`` is a usage error rather than a silent scaffold either way:
    creating a project is ``rayspec init``'s job.
    """
    checked_root(root)
    resolved = root.resolve() if root is not None else find_project_root(None)
    if not (resolved / PROJECT_DIR).is_dir():
        fail(
            f"{resolved} is not a rayspec project (no {PROJECT_DIR}/ directory)",
            hint="run `rayspec init` here first, or pass --root <project>",
        )
    return resolved


def agent_names(project: Path) -> list[str]:
    """Every agent name ``agent: <name>`` resolves to in ``project``, project and user scope.

    The loader looks in ``.rayspec/agents/`` first and in ``<RAYSPEC_HOME>/agents/`` second, so
    both are valid ``--agent`` targets.
    """
    return [ref.name for ref in discover_agents(project)]


def _checked_name(kind: str, name: str) -> str:
    validate = validate_identifier if kind == "workflow" else validate_name
    try:
        return validate(name)
    except ValueError as exc:
        fail(
            f"invalid {kind} name: {exc}",
            hint=f"file names are the {kind} name: .rayspec/{KINDS[kind][0]}/<name>.yaml",
        )
        raise AssertionError("unreachable") from None  # pragma: no cover


def _report(result: NewFile, next_steps: list[str]) -> None:
    out = console()
    verb = "created" if result.action == "created" else "overwrote"
    out.print(f"[green]{verb}[/green]  {result.relative}")
    out.print("\nnext steps:")
    for line in next_steps:
        out.print(f"  {escape(line)}")


def register(app: typer.Typer) -> None:
    new_app = typer.Typer(
        name="new",
        help="Add one workflow or agent to a project that already exists.",
        no_args_is_help=True,
        add_completion=False,
    )

    @new_app.command("workflow")
    def workflow(
        name: Annotated[str, typer.Argument(help="Workflow name (also the file name).")],
        agent: Annotated[
            str | None,
            typer.Option(
                "--agent",
                help="Use this named agent (`.rayspec/agents/<name>.yaml`) instead of an "
                "inline one.",
                show_default=False,
            ),
        ] = None,
        description: Annotated[
            str | None,
            typer.Option("--description", help="One-line `description:`.", show_default=False),
        ] = None,
        force: Annotated[
            bool, typer.Option("--force", help="Overwrite an existing workflow file.")
        ] = False,
        root: RootOption = None,
    ) -> None:
        """Add `.rayspec/workflows/<name>.yaml` to this project."""
        checked = _checked_name("workflow", name)
        checked_agent = _checked_name("agent", agent) if agent is not None else None
        project = project_root_for(root)
        if checked_agent is not None:
            known = agent_names(project)
            if checked_agent not in known:
                match = suggest(checked_agent, known)
                message = f"unknown agent {checked_agent!r}"
                if match is not None:
                    message += f"; did you mean {match!r}?"
                fail(
                    message,
                    hint=f"write it first with `rayspec new agent {checked_agent}`, or see what "
                    "this project has with `rayspec agents`",
                )
        text = workflow_text(checked, agent=checked_agent, description=description or "")
        try:
            result = write_new(project, "workflow", checked, text, force=force)
        except FileExistsError as exc:
            fail(f"{exc}", hint="pass --force to overwrite it")
            return  # unreachable: fail() raises typer.Exit
        except OSError as exc:
            fail(f"cannot write the workflow: {exc}")
            return  # unreachable
        _report(
            result,
            [
                f"rayspec validate {checked}                 # schema, graph, references",
                f"rayspec plan {checked}                     # inputs, agents/models, step order",
                f"rayspec run {checked} --dry-run            # scripted agents, no login needed",
            ],
        )

    @new_app.command("agent")
    def agent_cmd(
        name: Annotated[str, typer.Argument(help="Agent name (also the file name).")],
        force: Annotated[
            bool, typer.Option("--force", help="Overwrite an existing agent file.")
        ] = False,
        root: RootOption = None,
    ) -> None:
        """Add `.rayspec/agents/<name>.yaml` — a reusable agent any workflow can reference."""
        checked = _checked_name("agent", name)
        project = project_root_for(root)
        try:
            result = write_new(project, "agent", checked, agent_text(checked), force=force)
        except FileExistsError as exc:
            fail(f"{exc}", hint="pass --force to overwrite it")
            return  # unreachable: fail() raises typer.Exit
        except OSError as exc:
            fail(f"cannot write the agent: {exc}")
            return  # unreachable
        _report(
            result,
            [
                "rayspec agents                          # the resolved provider/model per agent",
                f"rayspec new workflow <name> --agent {checked}   # a workflow that uses it",
            ],
        )

    app.add_typer(new_app)
