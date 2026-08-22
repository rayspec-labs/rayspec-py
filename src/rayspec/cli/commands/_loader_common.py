# SPDX-License-Identifier: Apache-2.0
"""Helpers shared by the commands: roots and config, the ``--output`` flag, one JSON rendering.

Private module (underscore → not auto-registered). It began as the helpers of the read-only
loader commands (``workflows``, ``agents``, ``validate``, ``plan``) and is now where the CLI's
shared presentation lives: the two Rich consoles, ``fail``/``error_lines``, and the single place
that decides what a ``--json`` document and one line of a ``--json`` stream look like. Everything
imports it, so nothing here may import a command module. Provider-registry and templating imports
stay lazy, so a command still answers when one of those modules is not installed.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import click
import typer
import typer.core
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from rayspec.actor import ACTOR_ENV
from rayspec.config import (
    Config,
    ConfigError,
    env_paths,
    load_config,
    load_env,
    parse_env_text,
    rayspec_home,
)
from rayspec.errors import RayspecError
from rayspec.loader import discover_workflows, find_project_root
from rayspec.loader.discovery import YAML_SUFFIXES
from rayspec.loader.loader import import_optional
from rayspec.loader.validate import CapabilitiesFor, TemplateChecker
from rayspec.procenv import env_file_origin
from rayspec.providers.base import ProviderCapabilities
from rayspec.schema import SchemaError

RootOption = Annotated[
    Path | None,
    typer.Option(
        "--root",
        help="Project root (the directory containing .rayspec/). Default: walk up from the cwd.",
        show_default=False,
    ),
]
AllowUnsupportedOption = Annotated[
    bool,
    typer.Option("--allow-unsupported", help="Downgrade capability mismatches to warnings."),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


class OutputFormat(StrEnum):
    """``--output`` values: how a command presents its result."""

    table = "table"
    json = "json"


OutputOption = Annotated[
    OutputFormat | None,
    typer.Option(
        "--output",
        help="Presentation: `table` (default) or `json` (`--json` is the older spelling).",
        show_default=False,
    ),
]


def resolve_output(output: OutputFormat | None, json_: bool) -> bool:
    """Whether to print machine-readable JSON, from the two spellings of the same choice.

    ``--json`` predates ``--output`` and is in the docs, the packaged skill, tests and users'
    scripts, so it keeps working untouched: it is exactly ``--output json``. Passing both is fine
    while they agree and a usage error when they do not — silently letting one win would print a
    table into a pipe that asked for JSON.
    """
    if output is None:
        return json_
    if json_ and output is not OutputFormat.json:
        fail(
            f"--json and --output {output.value} disagree",
            hint="--json is the older spelling of --output json; pass one of them",
        )
    return output is OutputFormat.json


CAPABILITY_SKIP_WARNING = "capability checks skipped (providers registry not available)"


@dataclass(slots=True)
class Context:
    """Resolved roots + config for one command invocation."""

    project_root: Path
    home: Path
    config: Config


def short_path(path: Path, ctx: Context) -> str:
    """Render ``path`` relative to the project root (or as ``~/.rayspec/...``) when possible."""
    try:
        return path.relative_to(ctx.project_root).as_posix()
    except ValueError:
        pass
    try:
        return "~/.rayspec/" + path.relative_to(ctx.home).as_posix()
    except ValueError:
        return str(path)


def looks_like_path(target: str) -> bool:
    """The loader's rule for "``target`` is a file path, not a discovered name": a YAML suffix, a
    path separator or a leading dot (mirrors :mod:`rayspec.loader.loader`)."""
    return (
        target.endswith(YAML_SUFFIXES)
        or os.sep in target
        or "/" in target
        or target.startswith(".")
    )


def workflow_label(target: str, ctx: Context) -> str | None:
    """The label (``.rayspec/workflows/<name>.yaml``) of a workflow *target* without loading it.

    ``target`` is resolved the way the loader does: a path (see :func:`looks_like_path`) against
    the cwd, then the project root; anything else by discovered name — a bare name never refers
    to a cwd file of that name. ``None`` when nothing matches. Used to fill the ``path`` of a
    ``validate --json`` row when the workflow fails to load.
    """
    if looks_like_path(target):
        candidate = Path(target).expanduser()
        bases = [Path()] if candidate.is_absolute() else [Path.cwd(), ctx.project_root]
        for base in bases:
            if (base / candidate).is_file():
                return short_path((base / candidate).resolve(), ctx)
        return None
    for ref in discover_workflows(ctx.project_root, home=ctx.home):
        if ref.name == target:
            return short_path(ref.path, ctx)
    return None


def error_entries(exc: RayspecError) -> list[str]:
    """One entry per problem of a load error.

    A :class:`SchemaError` carries several ``<location>: <message>`` problems (unknown fields,
    bad identifiers …) which are reported one per entry, each prefixed with ``<file>:<line>``
    when the loader knew the line and with the file alone otherwise; any other error is
    one entry (multi-line messages such as the unsupported-feature block stay intact).
    """
    if isinstance(exc, SchemaError):
        if exc.problems:
            return [p.rendered() for p in exc.problems]
        prefix = f"{exc.source}: " if exc.source else ""
        return [prefix + e for e in exc.errors]
    return [str(exc)]


def _line_of(location: str | None) -> int | None:
    """The ``<line>`` of a ``<file>:<line>`` location string, when it ends in one."""
    if location is None:
        return None
    _, _, tail = location.rpartition(":")
    return int(tail) if tail.isdigit() else None


def error_problems(exc: RayspecError, *, path: str) -> list[dict[str, Any]]:
    """One ``--json`` object per problem of a load error.

    ``path`` is the fallback file the problems are attributed to (the target's label), so every
    object carries a non-null ``path`` even when the document never got far enough to name one.
    """
    if isinstance(exc, SchemaError):
        if exc.problems:
            return [{**p.to_json(), "path": p.source or path} for p in exc.problems]
        return [
            {
                "path": exc.source or path,
                "line": None,
                "location": None,
                "field": None,
                "message": entry,
                "hint": exc.hint,
            }
            for entry in exc.errors
        ]
    location = getattr(exc, "location", None)
    return [
        {
            "path": path,
            "line": _line_of(location),
            "location": location,
            "field": None,
            "message": str(exc),
            "hint": exc.hint,
        }
    ]


#: How :func:`rayspec.loader.validate.validate_workflow` appends a location to a message:
#: ``<where>: <message> (at <file>:<line>)``. The file is the document the problem is IN, which
#: for an ``include:``d step is not the document being validated.
_MESSAGE_LOCATION = re.compile(r" \(at (?P<location>(?P<path>.+):(?P<line>\d+))\)$")


def message_problems(messages: list[str], *, path: str) -> list[dict[str, Any]]:
    """One ``--json`` problem object per validation message (graph/reference/capability errors).

    These messages are produced by :func:`rayspec.loader.validate_workflow` and quote their own
    ``(at <file>:<line>)`` where one is known — for a problem inside an ``include:``d document
    that file is NOT ``path``. The quoted location fills ``path``/``line``/``location`` so the
    ``--json`` jump target is the document the problem is really in; ``path`` (the workflow being
    validated) is only the fallback for a message that carries no location.
    """
    out: list[dict[str, Any]] = []
    for message in messages:
        match = _MESSAGE_LOCATION.search(message)
        out.append(
            {
                "path": match.group("path") if match else path,
                "line": int(match.group("line")) if match else None,
                "location": match.group("location") if match else None,
                "field": None,
                "message": message,
                "hint": None,
            }
        )
    return out


#: Commands that execute workflow steps and therefore apply the project ``.rayspec/.env``;
#: every other command loads only ``~/.rayspec/.env``.
EXECUTION_COMMANDS: frozenset[str] = frozenset({"run", "resume", "approve", "reject"})


def _current_click_context() -> Any:
    """The active click context, or ``None`` outside a CLI invocation.

    Typer ≥ 0.20 ships a vendored click (``typer._click``) whose context stack is separate from
    the installed ``click`` package's — ``click.get_current_context(silent=True)`` returns
    ``None`` while a Typer app built on the vendored copy is running (typer 0.27 / click 8.4).
    Both stacks are probed; the vendored module exposes the getter in its ``globals`` submodule.
    """
    for module in (getattr(typer.core, "_click", None), click):
        if module is None:
            continue
        getter = getattr(module, "get_current_context", None)
        if getter is None:
            try:
                getter = importlib.import_module(f"{module.__name__}.globals").get_current_context
            except (ImportError, AttributeError):
                continue
        ctx = getter(silent=True)
        if ctx is not None:
            return ctx
    return None


def invoked_command() -> str | None:
    """Name of the top-level Typer command being executed (``run``, ``doctor`` …) or ``None``
    outside a CLI invocation (tests calling helpers directly)."""
    ctx = _current_click_context()
    while ctx is not None:
        name = getattr(ctx, "info_name", None)
        parent = getattr(ctx, "parent", None)
        if name and parent is not None and getattr(parent, "parent", None) is None:
            return str(name)
        if parent is None:
            break
        ctx = parent
    return None


def make_context(root: Path | None, *, project_env: bool | None = None) -> Context:
    """Resolve the project root / home, load config and ``.env`` files.

    An explicit ``--root`` that is not a directory is a usage error (exit 2) — a typo must not
    look like an empty project. A malformed ``config.yaml``/``.env`` (either layer) is printed
    as ``error: <path>:<line>: …`` with exit 2 — never a traceback.

    ``project_env`` decides whether the checkout's ``.rayspec/.env`` is applied to the process
    environment: ``None`` (default) applies it only for the execution commands
    (:data:`EXECUTION_COMMANDS`, detected from the click context); ``~/.rayspec/.env`` is always
    applied. Both files are applied in ONE :func:`load_env` call so the documented precedence
    holds (project wins over the home file; neither overrides a variable the shell already set).
    When project variables are applied a dim one-line notice NAMING them goes to stderr — a
    count alone does not let anybody see that the one variable applied was ``RAYSPEC_ACTOR``.

    A ``RAYSPEC_ACTOR`` from either file gets a real warning, because it is the one variable
    these files may not decide: see :func:`warn_about_declared_actor`.
    """
    if root is not None and not root.is_dir():
        fail(f"--root {str(root)!r} is not a directory")
    project_root = find_project_root(root)
    home = rayspec_home()
    if project_env is None:
        project_env = invoked_command() in EXECUTION_COMMANDS
    try:
        applied = load_env(project_root, home=home, include_project=project_env)
        if project_env:
            names = sorted(set(applied) & _project_env_keys(project_root, home))
            if names:
                err_console().print(
                    Text(
                        f"env: loaded {len(names)} variable{'s' if len(names) != 1 else ''} "
                        f"from .rayspec/.env (project): {_named(names)}",
                        style="dim",
                    )
                )
        warn_about_declared_actor()
        config = load_config(project_root, home=home)
    except ConfigError as exc:
        fail(str(exc), hint=exc.hint)
        raise AssertionError("unreachable") from None  # pragma: no cover
    return Context(project_root=project_root, home=home, config=config)


#: Longest list of variable names printed in full before the notice says "and N more".
_MAX_NAMED_ENV = 8


def _named(names: list[str]) -> str:
    """``A, B, C`` — names only, never values, and never more than :data:`_MAX_NAMED_ENV`."""
    if len(names) <= _MAX_NAMED_ENV:
        return ", ".join(names)
    shown = ", ".join(names[:_MAX_NAMED_ENV])
    return f"{shown} and {len(names) - _MAX_NAMED_ENV} more"


def warn_about_declared_actor() -> None:
    """Say out loud that a ``.env``'s ``RAYSPEC_ACTOR`` is not who rayspec thinks you are.

    Both ``.env`` files are files a workflow step can write — ``$RAYSPEC_HOME`` is exported into
    every step, and the project file sits in the tree the run works in — so neither may name the
    person a decision is attributed to. The value is refused, not applied on top of; the warning
    exists so that somebody who put it there on purpose learns why nothing happened, and so that
    somebody who did NOT put it there sees that something did.
    """
    origin = env_file_origin(ACTOR_ENV)
    if origin is None:
        return
    err_console().print(
        Text(
            f"warning: {ACTOR_ENV} in {origin} is not used as an identity — a workflow step can "
            f"write that file. Export {ACTOR_ENV} in the shell that runs rayspec instead.",
            style="yellow",
        )
    )


def _project_env_keys(project_root: Path, home: Path) -> set[str]:
    """Keys defined in ``<project>/.rayspec/.env`` (empty when the file is absent/unreadable —
    :func:`load_env` has already reported an unreadable file)."""
    _, project_path = env_paths(project_root, home)
    try:
        return set(parse_env_text(project_path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        return set()


@dataclass(slots=True)
class CapabilitySource:
    """Capability lookup obtained from the provider registry (when importable)."""

    capabilities_for: CapabilitiesFor | None
    provider_ids: list[str] = field(default_factory=list)
    warning: str | None = None


def capability_source() -> CapabilitySource:
    """Build ``capabilities_for`` from :mod:`rayspec.providers.registry`, lazily and defensively."""
    registry = import_optional("rayspec.providers.registry")
    if registry is None:  # the registry is a parallel scope; fall back to a warning
        return CapabilitySource(None, [], CAPABILITY_SKIP_WARNING)
    registrations = list(registry.list_registrations())
    table: dict[str, ProviderCapabilities] = {r.id: r.capabilities for r in registrations}

    def lookup(provider_id: str) -> ProviderCapabilities | None:
        if provider_id in table:
            return table[provider_id]
        try:
            reg = registry.get_registration(provider_id)
        except (LookupError, RayspecError):  # unknown provider id
            return None
        table[provider_id] = reg.capabilities
        return reg.capabilities

    return CapabilitySource(lookup, sorted(table))


def template_checker() -> TemplateChecker | None:
    """Return the templating scope's engine when it is importable, else ``None``."""
    templating = import_optional("rayspec.templating")
    if templating is None:
        return None
    engine = templating.TemplateEngine()
    if all(
        callable(getattr(engine, name, None))
        for name in ("compile_template", "compile_expr", "references")
    ):
        return engine
    return None


def stdout_is_tty() -> bool:
    """Whether stdout is a terminal — the one probe every renderer here asks.

    A closed or replaced stdout (a test runner's, a broken pipe) counts as "not a terminal": the
    machine-readable rendering is the safe answer when nobody can say who is reading.
    """
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):  # detached / closed stdout
        return False


def console() -> Console:
    """A Rich console on the *current* stdout (CliRunner-safe), wide when not a terminal."""
    is_tty = stdout_is_tty()
    width = shutil.get_terminal_size().columns if is_tty else 200
    return Console(file=sys.stdout, width=width, highlight=False, soft_wrap=True)


def err_console() -> Console:
    return Console(file=sys.stderr, highlight=False, soft_wrap=True, width=200)


#: Separators of the piped rendering: no spaces at all. This is what pydantic's
#: ``model_dump_json`` writes, so ``run.json``, ``events.jsonl`` and every ``--json`` document
#: rayspec prints are one format rather than three that happen to parse the same.
_COMPACT: tuple[str, str] = (",", ":")


def json_text(payload: Any) -> str:
    """Render one ``--json`` / ``--output json`` **document** — the single rule, for every command.

    Indented by two spaces when stdout is a terminal (a person is reading it), compact when it is
    redirected or piped (a program is). Nothing else varies: non-ASCII is written as itself
    (``ä``, not ``\u00e4``), key order is the payload's, and a value the payload builder left
    unserialisable is rendered as its ``str()`` rather than taking the command down after it has
    already done its work.

    The rule is deliberately not per command: ``rayspec workflows --json | jq`` and ``rayspec
    runs --json | jq`` used to disagree about whether they emit one line or twenty, which makes
    every shell pipeline around them a command-specific special case.
    """
    if stdout_is_tty():
        return json.dumps(payload, ensure_ascii=False, default=str, indent=2)
    return json.dumps(payload, ensure_ascii=False, default=str, separators=_COMPACT)


def print_json(payload: Any) -> None:
    """Print one ``--json`` document on stdout in the house rendering (:func:`json_text`).

    ``soft_wrap`` is not optional here: Rich would otherwise fold a long line to the console
    width, and a compact document has no spaces to fold at — the break lands inside a string
    and the document stops being JSON.
    """
    console().print(json_text(payload), markup=False, highlight=False, soft_wrap=True)


def json_line(payload: Any) -> str:
    """Render one record of a line-delimited stream (``run --json``, ``logs --json``).

    Always compact, terminal or not: the format's promise is one object per line, and
    ``rayspec run … --json | tail -1 | jq .exit_code`` reads a fragment as soon as a record is
    allowed to wrap. Otherwise identical to :func:`json_text`.
    """
    return json.dumps(payload, ensure_ascii=False, default=str, separators=_COMPACT)


def new_table(*, title: str | None = None, show_header: bool = True) -> Table:
    """The one rayspec table: no box, no edges, a bold header, a left-justified title.

    Listings are read on a terminal and in a redirected file, and the file is the demanding
    reader: it gets grepped, diffed against yesterday's and pasted into an issue. Borders make
    all three worse and their width moves with the data, so no table draws any — and no command
    picks its own, which is the part that kept ``rayspec doctor`` and ``rayspec runs`` from
    looking like the same program.

    Only ``title`` and ``show_header`` are choices; a caller that wants a different box wants a
    different tool.
    """
    return Table(
        title=title,
        title_justify="left",
        show_header=show_header,
        header_style="bold",
        box=None,
        show_edge=False,
        pad_edge=False,
    )


def fail(message: str, *, code: int = 2, hint: str | None = None) -> None:
    """Print an error (and hint) to stderr and exit with ``code``.

    ``message``/``hint`` are rendered as plain text — they often quote run data or user input
    (``[stub] …``), which must never be interpreted as Rich markup.
    """
    out = err_console()
    out.print(Text.assemble(("error:", "red"), " ", message))
    if hint:
        out.print(Text(f"hint: {hint}", style="dim"))
    raise typer.Exit(code=code)


def error_lines(items: list[str], *, json_mode: bool = False, kind: str = "errors") -> None:
    """Print problems the way :func:`fail` does — ``error: <msg>`` per item on stderr — or, in
    ``--json`` mode, one JSON object ``{"error": <kind>, "errors": [...]}`` on stdout.

    Items quote user text (input values, step paths like ``build[2]/check``, regexes): every line
    is escaped so Rich never reads it as markup.
    """
    if json_mode:
        print_json({"error": kind, "errors": list(items)})
        return
    out = err_console()
    for item in items:
        first, *rest = item.splitlines()
        out.print(f"[red]error:[/red] {escape(first)}", highlight=False)
        for line in rest:
            out.print(f"       {escape(line)}", highlight=False)


def report_lines(
    title: str, items: list[str], *, style: str, printer: Callable[[str], None]
) -> None:
    """Print ``title`` + each item (multi-line items indented) using ``printer``.

    Items are error/warning messages that quote user text (identifier regexes such as
    ``^[a-z][a-z0-9_]*$``, JSON, step paths like ``build[2]/check``): they are escaped so Rich
    never reads them as markup.
    """
    if not items:
        return
    printer(f"[{style}]{title}[/{style}]")
    for item in items:
        first, *rest = item.splitlines()
        printer(f"  - {escape(first)}")
        for line in rest:
            printer(f"    {escape(line)}")


__all__ = [
    "CAPABILITY_SKIP_WARNING",
    "EXECUTION_COMMANDS",
    "AllowUnsupportedOption",
    "CapabilitySource",
    "Context",
    "JsonOption",
    "OutputFormat",
    "OutputOption",
    "RootOption",
    "capability_source",
    "console",
    "err_console",
    "error_entries",
    "error_lines",
    "error_problems",
    "fail",
    "invoked_command",
    "json_line",
    "json_text",
    "looks_like_path",
    "make_context",
    "message_problems",
    "new_table",
    "print_json",
    "report_lines",
    "resolve_output",
    "short_path",
    "stdout_is_tty",
    "template_checker",
    "workflow_label",
]
