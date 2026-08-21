# SPDX-License-Identifier: Apache-2.0
"""The `rayspec` Typer app. Commands live in :mod:`rayspec.cli.commands` (auto-discovered)."""

from __future__ import annotations

import importlib
import pkgutil

import typer

from rayspec.cli import commands as _commands_pkg

app = typer.Typer(
    name="rayspec",
    help="Declarative agent workflows for coding agents (Claude Agent SDK + Codex SDK).",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


@app.callback()
def _root() -> None:
    """Declarative agent workflows for coding agents."""


def discovered_command_modules() -> list[str]:
    return sorted(
        f"{_commands_pkg.__name__}.{info.name}"
        for info in pkgutil.iter_modules(_commands_pkg.__path__)
        if not info.name.startswith("_")
    )


def _register_all() -> None:
    for module_name in discovered_command_modules():
        module = importlib.import_module(module_name)
        register = getattr(module, "register", None)
        if callable(register):
            register(app)


_register_all()


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
