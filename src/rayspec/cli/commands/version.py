# SPDX-License-Identifier: Apache-2.0
"""`rayspec version` and the top-level `--version` / `-V` flag."""

from __future__ import annotations

from typing import Annotated

import typer

from rayspec import __version__


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"rayspec {__version__}")
        raise typer.Exit()


def register(app: typer.Typer) -> None:
    @app.command()
    def version() -> None:
        """Print the rayspec version."""
        typer.echo(f"rayspec {__version__}")

    # the root callback is re-registered here (app.py stays untouched) to add the eager flag
    @app.callback()
    def _root(
        version_flag: Annotated[
            bool,
            typer.Option(
                "--version",
                "-V",
                help="Print the rayspec version and exit.",
                callback=_print_version,
                is_eager=True,
            ),
        ] = False,
    ) -> None:
        """Declarative agent workflows for coding agents."""
