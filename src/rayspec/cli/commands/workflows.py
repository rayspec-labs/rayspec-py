# SPDX-License-Identifier: Apache-2.0
"""`rayspec workflows` — every workflow a name resolves to; `workflows eject` copies a bundled one.

``rayspec workflows`` (no subcommand) lists project (``.rayspec/workflows/``), user
(``~/.rayspec/workflows/``) and bundled workflows in that order of precedence. A project or user
file with the same stem shadows the bundled one and the listing says so (``source: overridden``).
``rayspec workflows eject <name>`` copies a bundled definition into the project under a header
naming the rayspec version and the digest of the bundled bytes — which is how the listing later
notices that the bundled workflow moved on (``note: … has changed since``).

Presentation and argument plumbing only: discovery lives in :mod:`rayspec.loader.discovery`, the
library, its labels and the eject header in :mod:`rayspec.loader.bundled`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from rayspec import __version__
from rayspec.cli._docs import docs_url
from rayspec.cli.commands._loader_common import (
    Context,
    JsonOption,
    OutputOption,
    RootOption,
    console,
    fail,
    group_root,
    make_context,
    new_table,
    print_json,
    resolve_output,
    short_path,
)
from rayspec.errors import RayspecError
from rayspec.loader import WorkflowRef, load_workflow
from rayspec.loader.bundled import bundled_digest, is_bundled, parse_eject_header, render_ejected
from rayspec.schema.base import suggest

#: Printed by ``workflows`` and ``validate`` when a project has no workflow yet.
EMPTY_PROJECT_HINT = (
    "run `rayspec init` to scaffold a first workflow, or create .rayspec/workflows/<name>.yaml "
    f"— examples: {docs_url('docs/examples.md')}"
)

#: Where an ejected copy goes, relative to the project root.
EJECT_DIR = Path(".rayspec") / "workflows"


def source_of(ref: WorkflowRef) -> str:
    """The ``source`` column: ``overridden`` for a project/user file that shadows a bundled one."""
    return "overridden" if ref.overrides is not None else ref.scope


def input_rows(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{name: {type, required, default, enum, description, secret}}`` from a raw ``inputs:``
    mapping — the defaults of ``InputSpec`` without its strictness: a spec that is not a mapping
    is skipped and an odd field is coerced, never an error, because this is a listing."""
    rows: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not isinstance(spec, Mapping):
            continue
        kind, enum, description = spec.get("type"), spec.get("enum"), spec.get("description")
        rows[name] = {
            "type": kind if isinstance(kind, str) else "string",
            "required": bool(spec.get("required", False)),
            "default": spec.get("default"),
            "enum": list(enum) if isinstance(enum, list) else None,
            "description": description if isinstance(description, str) else None,
            "secret": bool(spec.get("secret", False)),
        }
    return rows


def eject_state(ref: WorkflowRef) -> dict[str, Any] | None:
    """``{version, sha256, bundled_changed}`` when ``ref`` shadows a bundled workflow and carries
    an eject header; ``None`` for every other ref (a hand-written shadow included)."""
    if ref.overrides is None:
        return None
    try:
        header = parse_eject_header(ref.path.read_text(encoding="utf-8"))
        current = bundled_digest(ref.overrides)
    except (OSError, UnicodeDecodeError):
        return None
    if header is None:
        return None
    return {
        "version": header.version,
        "sha256": header.sha256,
        "bundled_changed": header.sha256 != current,
    }


def json_row(ref: WorkflowRef) -> dict[str, Any]:
    """One ``--json`` row — additive over the historical five keys (``name`` … ``error``)."""
    return {
        "name": ref.name,
        "scope": ref.scope,
        "source": source_of(ref),
        "description": ref.description,
        "path": str(ref.path),
        "error": ref.error,
        "overrides": None if ref.overrides is None else str(ref.overrides),
        "ejected": eject_state(ref),
        "inputs": input_rows(ref.inputs),
    }


def bundled_includes(source: Path, ctx: Context) -> list[str]:
    """The bundled workflows ``source`` includes, by discovered name (file stem) — what someone
    who just ejected ``source`` may want to eject next. A document that does not load names
    nothing: the copy was written, this is a hint."""
    try:
        resolved = load_workflow(
            source, project_root=ctx.project_root, home=ctx.home, config=ctx.config
        )
    except (RayspecError, OSError):
        return []
    names: list[str] = []
    for body in resolved.includes.values():
        if is_bundled(body.path) and body.path.stem not in names:
            names.append(body.path.stem)
    return names


def list_workflows(*, root: Path | None, json_: bool) -> None:
    ctx = make_context(root)
    refs = ctx.workflow_refs()
    if json_:
        print_json([json_row(r) for r in refs])
        return
    out = console()
    if not refs:  # not even the library: a broken install, still not a traceback
        out.print(
            f"no workflows found under {ctx.project_root / '.rayspec' / 'workflows'} "
            f"or {ctx.home / 'workflows'}"
        )
        out.print(f"[dim]hint: {EMPTY_PROJECT_HINT}[/dim]", highlight=False)
        return
    table = new_table()
    table.add_column("name", style="bold")
    table.add_column("source")
    table.add_column("description")
    table.add_column("path", style="dim")
    broken: list[tuple[str, str]] = []
    for r in refs:
        if r.error is None:
            desc = r.description
        else:
            desc = "[red](parse error — see rayspec validate)[/red]"
            broken.append((r.name, r.error))
        # a bundled file lives in the installed package: a path there is noise, not a location
        path = "" if r.scope == "bundled" else short_path(r.path, ctx)
        table.add_row(r.name, source_of(r), desc, path)
    out.print(table)
    for name, error in broken:
        first = error.splitlines()[0] if error else "cannot load"
        first = first.replace(str(ctx.project_root) + "/", "").replace(str(ctx.home), "~/.rayspec")
        out.print(f"[red]error:[/red] {name}: {first}", highlight=False)
    for r in refs:
        state = eject_state(r)
        if state is not None and state["bundled_changed"]:
            out.print(
                f"[dim]note: {r.name} was ejected from rayspec {state['version']}; "
                "the bundled workflow has changed since[/dim]",
                highlight=False,
            )
    if all(r.scope == "bundled" for r in refs):
        out.print(
            f"[dim]hint: no project workflows yet — {EMPTY_PROJECT_HINT}; "
            "`rayspec workflows eject <name>` copies a bundled one[/dim]",
            highlight=False,
        )


def register(app: typer.Typer) -> None:
    workflows_app = typer.Typer(
        name="workflows",
        # no help= : the callback docstring is the group help, so `rayspec workflows --help`
        # keeps leading with what the bare invocation does (the `runs` pattern)
        no_args_is_help=False,
        add_completion=False,
    )

    @workflows_app.callback(invoke_without_command=True)
    def workflows(
        ctx: typer.Context,
        root: RootOption = None,
        json_: JsonOption = False,
        output: OutputOption = None,
    ) -> None:
        """List workflows: .rayspec/workflows/, ~/.rayspec/workflows/ and the bundled library."""
        ctx.obj = root
        if ctx.invoked_subcommand is not None:
            # only --root is forwarded (ctx.obj); the listing flags would be silently dropped
            given = [
                flag for flag, used in (("--json", json_), ("--output", output is not None)) if used
            ]
            if given:
                fail(
                    f"{', '.join(given)} belongs to the `rayspec workflows` listing, not to "
                    f"`{ctx.invoked_subcommand}`",
                    hint="put it after the subcommand: rayspec workflows "
                    f"{ctx.invoked_subcommand} ... {given[0]}",
                )
            return
        list_workflows(root=root, json_=resolve_output(output, json_))

    @workflows_app.command("eject")
    def eject(
        ctx: typer.Context,
        name: Annotated[str, typer.Argument(help="A bundled workflow (see `rayspec workflows`).")],
        force: Annotated[
            bool,
            typer.Option("--force", help="Overwrite an existing .rayspec/workflows/<name>.yaml."),
        ] = False,
        root: RootOption = None,
    ) -> None:
        """Copy a bundled workflow into .rayspec/workflows/ so this project can edit it."""
        root = group_root(ctx, root)
        # --root is checked first and taken literally as the place to write: a mistyped path
        # is exit 2 here, never a directory created somewhere; without it the project is the
        # walk-up every command does (a repository with no .rayspec/ yet is fine — it gets one)
        context = make_context(root)
        project = root.resolve() if root is not None else context.project_root
        refs = context.workflow_refs()
        ref = next((r for r in refs if r.name == name), None)
        if ref is None:
            ejectable = [r.name for r in refs if r.scope == "bundled" or r.overrides is not None]
            match = suggest(name, ejectable)
            message = f"unknown workflow {name!r}"
            if match is not None:
                message += f"; did you mean {match!r}?"
            fail(message, hint="run `rayspec workflows` to list the bundled workflows")
            raise AssertionError("unreachable")  # pragma: no cover
        source = ref.path if ref.scope == "bundled" else ref.overrides
        if source is None:
            fail(
                f"{name!r} is not a bundled workflow (it is a {ref.scope} workflow at "
                f"{short_path(ref.path, context)})",
                hint="only the workflows rayspec ships can be ejected; `rayspec workflows` "
                "shows which those are",
            )
            raise AssertionError("unreachable")  # pragma: no cover
        relative = (EJECT_DIR / f"{name}.yaml").as_posix()
        target = project / relative
        if target.is_symlink():
            fail(
                f"{relative} is a symbolic link, expected a file (or nothing)",
                hint="an ejected copy is a file inside the project; remove the link and re-run",
            )
        if target.is_dir():
            fail(f"{relative} is a directory, expected a file (or nothing)")
        existed = target.exists()
        if existed and not force:
            fail(f"{relative} already exists", hint="pass --force to overwrite it")
        text = source.read_text(encoding="utf-8")
        rendered = render_ejected(name, text, version=__version__, digest=bundled_digest(source))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        out = console()
        verb = "overwrote" if existed else "ejected"
        out.print(
            f"{verb} {name} → {relative} (rayspec {__version__}); this copy now takes "
            "precedence over the bundled workflow",
            highlight=False,
        )
        for included in bundled_includes(source, context):
            out.print(
                f"[dim]note: it includes bundled {included}; `rayspec workflows eject "
                f"{included}` to customise that too[/dim]",
                highlight=False,
            )

    app.add_typer(workflows_app, name="workflows")


__all__ = [
    "EJECT_DIR",
    "EMPTY_PROJECT_HINT",
    "bundled_includes",
    "eject_state",
    "input_rows",
    "json_row",
    "list_workflows",
    "register",
    "source_of",
]
