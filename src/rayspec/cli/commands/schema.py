# SPDX-License-Identifier: Apache-2.0
"""`rayspec schema [workflow|run|events|stream] [--out DIR]` — the published JSON Schemas.

Prints one schema to stdout (pipe it into a file or an editor config) or writes the whole set
into a directory. Thin command over :mod:`rayspec.schemagen`, which generates every schema from
the Pydantic models — the checked-in copies under ``schemas/`` are the same bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from rayspec.cli.commands._loader_common import console, fail
from rayspec.schema.base import suggest
from rayspec.schemagen import (
    SCHEMA_KINDS,
    SCHEMA_SUBJECTS,
    modeline,
    schema_filename,
    schema_id,
    schema_text,
)

OutOption = Annotated[
    Path | None,
    typer.Option(
        "--out",
        help="Write the schema file(s) into this directory instead of printing.",
        show_default=False,
    ),
]


def _resolve(kind: str | None) -> tuple[str, ...]:
    """The kinds to act on; an unknown one exits 2 with a did-you-mean."""
    if kind is None:
        return SCHEMA_KINDS
    if kind in SCHEMA_KINDS:
        return (kind,)
    hint = suggest(kind, list(SCHEMA_KINDS))
    fail(
        f"unknown schema {kind!r}",
        hint=f"did you mean {hint!r}?" if hint else f"known: {', '.join(SCHEMA_KINDS)}",
    )
    raise AssertionError("unreachable")  # pragma: no cover


def register(app: typer.Typer) -> None:
    @app.command()
    def schema(
        kind: Annotated[
            str | None,
            typer.Argument(help=f"Which schema: {', '.join(SCHEMA_KINDS)}. Default: list them."),
        ] = None,
        out: OutOption = None,
    ) -> None:
        """Print or write the published JSON Schemas (workflow, run, events, stream)."""
        kinds = _resolve(kind)
        term = console()
        if out is not None:
            if out.exists() and not out.is_dir():
                fail(f"--out {str(out)!r} is not a directory")
            try:
                out.mkdir(parents=True, exist_ok=True)
                for one in kinds:
                    (out / schema_filename(one)).write_text(schema_text(one), encoding="utf-8")
            except OSError as exc:
                fail(f"could not write into {str(out)!r}: {exc}")
            names = ", ".join(schema_filename(one) for one in kinds)
            term.print(f"wrote {names} to {out}", markup=False, highlight=False)
            local = (out / schema_filename("workflow")).resolve().as_uri()
            term.print(
                f"[dim]editor modeline (local copy): {modeline(url=local)}[/dim]",
                highlight=False,
            )
            return
        if kind is None:
            term.print("[bold]published JSON Schemas[/bold] (JSON Schema 2020-12)")
            for one in kinds:
                term.print(f"  [bold]{one}[/bold]  {SCHEMA_SUBJECTS[one]}", highlight=False)
                term.print(f"    {schema_id(one)}", markup=False, highlight=False)
            term.print(f"[dim]workflow modeline: {modeline()}[/dim]", highlight=False)
            term.print(
                "[dim]print one: rayspec schema workflow  ·  write all: rayspec schema "
                "--out schemas/[/dim]"
            )
            return
        # json.dumps of the already-built document: never Rich markup, never re-highlighted
        term.print(
            json.dumps(json.loads(schema_text(kinds[0])), indent=2), markup=False, highlight=False
        )


__all__ = ["register"]
