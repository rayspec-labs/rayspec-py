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

import contextlib
import errno
import importlib
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import click
import typer
import typer.core
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markup import escape
from rich.segment import Segment
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
from rayspec.loader import WorkflowRef, discover_workflows, find_project_root
from rayspec.loader.bundled import bundled_label
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
    #: Memo for :meth:`workflow_refs`. Discovery answers a question about the project, not about
    #: the name being resolved, so it belongs to the invocation — one listing however many names
    #: the command was given.
    _refs: list[WorkflowRef] | None = None

    def workflow_refs(self) -> list[WorkflowRef]:
        """The project's discovered workflows, listed once per command invocation.

        ``rayspec validate`` (and `run`, `lock`, `trust`) resolves every name it was given
        against this list. Discovering per name made a project of N workflows cost N directory
        listings to validate — quadratic in a command whose work is linear.
        """
        if self._refs is None:
            self._refs = discover_workflows(self.project_root, home=self.home)
        return self._refs


def short_path(path: Path, ctx: Context) -> str:
    """Render ``path`` relative to the project root (or as ``~/.rayspec/...``) when possible;
    a file of the bundled library is ``<bundled>/<name>.yaml`` (the loader's label)."""
    bundled = bundled_label(path)
    if bundled is not None:
        return bundled
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


def workflow_target(target: str, ctx: Context) -> str | WorkflowRef:
    """``target`` as the loader should receive it: the discovered ref whenever it names one.

    :func:`rayspec.loader.load_workflow` builds a fresh loader per call, so a bare NAME makes it
    list the project again — once per workflow for a command that loads them all. Handing it the
    ref the invocation has already discovered skips that; a path (or a name that matches
    nothing, which the loader must still report in its own words) is passed through unchanged.
    """
    if looks_like_path(target):
        return target
    for ref in ctx.workflow_refs():
        if ref.name == target:
            return ref
    return target


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
    for ref in ctx.workflow_refs():
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


def group_root(ctx: typer.Context, root: Path | None) -> Path | None:
    """A subcommand's ``--root``, falling back to the one given before the subcommand name
    (``rayspec runs --root X diff a b``) — the group callback stashes it in ``ctx.obj``."""
    if root is not None:
        return root
    parent_root = getattr(ctx, "obj", None)
    return parent_root if isinstance(parent_root, Path) else None


def checked_root(root: Path | None) -> Path | None:
    """Apply the one ``--root`` rule and hand the option back unchanged.

    An explicit ``--root`` that is not an existing directory is a usage error (exit 2). That
    holds for the commands that WRITE a root (``init``, ``skill install``) exactly as for the
    ones that read one: a mistyped path must not quietly become a new directory tree nobody
    named, reported as a success — which is what ``rayspec init --root /typo`` used to do while
    ``rayspec validate --root /typo`` refused.

    Commands that resolve their root without :func:`make_context` call this first, so the rule
    lives in one place instead of being re-decided per command.
    """
    if root is not None and not root.is_dir():
        fail(f"--root {str(root)!r} is not a directory")
    return root


def make_context(root: Path | None, *, project_env: bool | None = None) -> Context:
    """Resolve the project root / home, load config and ``.env`` files.

    An explicit ``--root`` that is not a directory is a usage error (exit 2, :func:`checked_root`)
    — a typo must not look like an empty project. A malformed ``config.yaml``/``.env`` (either
    layer) is printed as ``error: <path>:<line>: …`` with exit 2 — never a traceback.

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
    checked_root(root)
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


def stdout_can_encode(text: str) -> bool:
    """Whether stdout's codec can write ``text`` as it stands — the second probe, like
    :func:`stdout_is_tty`.

    ``PYTHONIOENCODING=ascii``, a C/POSIX locale and the legacy Windows code pages all hand a
    process a stdout that is not UTF-8, and writing ``ä`` into one raises ``UnicodeEncodeError``
    from inside the write. A stream that will not name its codec is taken to be UTF-8, and an
    unknown codec name counts as "no": the escaped rendering is always readable.
    """
    if text.isascii():
        return True
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding, errors="strict")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _dumps(payload: Any, **kwargs: Any) -> str:
    """``json.dumps`` in the house style, escaped only when stdout cannot take the characters."""
    text = json.dumps(payload, ensure_ascii=False, default=str, **kwargs)
    if stdout_can_encode(text):
        return text
    return json.dumps(payload, ensure_ascii=True, default=str, **kwargs)


def json_text(payload: Any) -> str:
    """Render one ``--json`` / ``--output json`` **document** — the single rule, for every command.

    Indented by two spaces when stdout is a terminal (a person is reading it), compact when it is
    redirected or piped (a program is). Nothing else varies: key order is the payload's, and a
    value the payload builder left unserialisable is rendered as its ``str()`` rather than taking
    the command down after it has already done its work.

    Non-ASCII is written as itself (``ä``) — unless stdout cannot encode it
    (:func:`stdout_can_encode`), in which case the whole document falls back to ``\\uXXXX``
    escapes. Both spellings parse to the same payload, and a document nobody can print is worth
    less than an escaped one.

    The rule is deliberately not per command: ``rayspec workflows --json | jq`` and ``rayspec
    runs --json | jq`` used to disagree about whether they emit one line or twenty, which makes
    every shell pipeline around them a command-specific special case.
    """
    if stdout_is_tty():
        return _dumps(payload, indent=2)
    return _dumps(payload, separators=_COMPACT)


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
    allowed to wrap. Otherwise identical to :func:`json_text`, escapes on a stdout that cannot
    encode the characters included.
    """
    return _dumps(payload, separators=_COMPACT)


class _Listing(Table):
    """A :class:`~rich.table.Table` that ends its lines where the text ends.

    Rich pads every cell out to its column width. With a border that padding sits behind the
    right edge and nobody sees it; without one — and rayspec's listings have none — it becomes
    trailing whitespace on most lines of a redirected listing. That is the noise that makes
    ``git diff`` complain, an editor rewrite the file on save and a pasted snippet look wrong,
    on a file whose whole point is that it diffs cleanly against yesterday's.

    Stripping it here rather than at each print site is deliberate: a listing cannot be printed
    without going through the table it was built from, so there is no second way to emit one.
    """

    def _segments(self, console: Console, options: ConsoleOptions) -> Iterator[Segment]:
        """``Table``'s own rendering, flattened to segments."""
        for item in super().__rich_console__(console, options):
            if isinstance(item, Segment):
                yield item
            else:  # pragma: no cover — Table yields segments, but the protocol allows both
                yield from console.render(item, options)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render as ``Table`` does, minus the padding at the end of every line."""
        for line in Segment.split_lines(self._segments(console, options)):
            segments = list(line)
            while segments and not segments[-1].text.strip():
                segments.pop()
            if segments:
                last = segments[-1]
                segments[-1] = Segment(last.text.rstrip(), last.style, last.control)
            yield from segments
            yield Segment("\n")


def new_table(*, title: str | None = None, show_header: bool = True) -> Table:
    """The one rayspec table: no box, no edges, a bold header, a left-justified title.

    Listings are read on a terminal and in a redirected file, and the file is the demanding
    reader: it gets grepped, diffed against yesterday's and pasted into an issue. Borders make
    all three worse and their width moves with the data, so no table draws any — and no command
    picks its own, which is the part that kept ``rayspec doctor`` and ``rayspec runs`` from
    looking like the same program. Lines end where their text ends (:class:`_Listing`).

    Only ``title`` and ``show_header`` are choices; a caller that wants a different box wants a
    different tool.
    """
    return _Listing(
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


#: ``errno`` values that mean "whoever was reading stopped", not "the filesystem said no".
#: ``rayspec runs | head -1`` ends this way, and click's own ``main()`` already exits quietly
#: for it — :func:`error_boundary` must therefore let them past untouched.
_PIPE_ERRNOS: frozenset[int] = frozenset({errno.EPIPE, errno.ESHUTDOWN})


def _home_or_none() -> Path | None:
    """``RAYSPEC_HOME`` as a path, or ``None`` when it cannot be resolved at all."""
    try:
        return rayspec_home()
    except (OSError, RayspecError):  # pragma: no cover - it reads one environment variable
        return None


def _within(path: Path, home: Path) -> bool:
    """Whether ``path`` is ``home`` or lies below it — a symlinked home (``/tmp`` →
    ``/private/tmp`` on macOS) included."""
    if path == home or path.is_relative_to(home):
        return True
    try:
        return path.resolve().is_relative_to(home.resolve())
    except OSError:  # pragma: no cover - resolve() is non-strict
        return False


def filesystem_failure(exc: OSError) -> tuple[str, str | None]:
    """The ``(message, hint)`` an :class:`OSError` that reached the boundary is reported as.

    A path under ``RAYSPEC_HOME`` is named as what it is. Every run's record, events, outputs,
    locks and worktrees live there, and a bare ``Permission denied: /…/projects/local/x`` only
    becomes an answer once the line says which directory the reader is expected to fix.
    """
    reason = exc.strerror or str(exc)
    raw = exc.filename or exc.filename2
    path = Path(str(raw)) if raw else None
    home = _home_or_none()
    if path is not None and home is not None and _within(path, home):
        where = "" if path == home else f" ({path})"
        return (
            f"cannot use the rayspec home {home}: {reason}{where}",
            f"every run is recorded under it — check that {home} exists and is writable "
            "(RAYSPEC_HOME names it)",
        )
    detail = f"{reason}: {path}" if path is not None else reason
    return (detail, "check that the path exists and that you may read and write it")


@contextlib.contextmanager
def error_boundary() -> Iterator[None]:
    """Turn what escapes a command into the documented refusal: ``error: …`` on stderr, exit 2.

    Three kinds of failure end a command without a result and none may reach a user as a
    traceback: a :class:`~rayspec.errors.RayspecError` — what rayspec raises about a workflow, a
    run store, a workspace — an :class:`OSError` from the filesystem underneath it, and a
    :class:`UnicodeDecodeError` from a file that is not the text it was expected to be. Commands
    handle the cases they expect; this is the boundary for the ones they do not.

    ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``, so it escaped a boundary that
    named only the other two — and a workflow file holding a stray byte is an ordinary thing to
    find in a checkout, not a bug in rayspec. It is caught by name rather than by widening to
    ``Exception``, which would swallow the failures that ARE bugs and should still be seen.

    It is deliberately not a per-command decision. ``rayspec run`` mapped a store error to exit 2
    on the path that takes a lock and left the ``--dry-run`` path — the one ``rayspec init``
    tells a new user to run first — to end in a traceback and exit 1, which is the code that
    means "the workflow failed" for a run that was never created.
    """
    try:
        yield
    except RayspecError as exc:
        fail(str(exc), hint=exc.hint)
    except OSError as exc:
        if exc.errno in _PIPE_ERRNOS:
            raise
        message, hint = filesystem_failure(exc)
        fail(message, hint=hint)
    except UnicodeDecodeError as exc:
        # the offending bytes are deliberately not echoed: they are not text, and printing them
        # is how a terminal ends up interpreting a stray escape sequence out of somebody's file
        fail(
            f"a file rayspec read is not valid UTF-8 text: {exc.reason} at byte {exc.start}",
            hint="workflows, agents, prompts, stub scripts and config files are all UTF-8 text — "
            "check the file the command was reading",
        )


class ErrorBoundaryGroup(typer.core.TyperGroup):
    """The ``rayspec`` root group, with :func:`error_boundary` around everything it invokes.

    Click runs every command from inside the root group's ``invoke`` — sub-groups (``rayspec new
    workflow``, ``rayspec runs diff``) and installed CLI plugins included — so this one class is
    the whole boundary, and a command added later is covered without having to remember it.
    """

    def invoke(self, ctx: Any) -> Any:
        with error_boundary():
            return super().invoke(ctx)


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
    "ErrorBoundaryGroup",
    "JsonOption",
    "OutputFormat",
    "OutputOption",
    "RootOption",
    "capability_source",
    "checked_root",
    "console",
    "err_console",
    "error_boundary",
    "error_entries",
    "error_lines",
    "error_problems",
    "fail",
    "filesystem_failure",
    "group_root",
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
    "stdout_can_encode",
    "stdout_is_tty",
    "template_checker",
    "workflow_label",
    "workflow_target",
]
