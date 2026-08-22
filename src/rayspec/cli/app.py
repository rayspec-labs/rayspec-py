# SPDX-License-Identifier: Apache-2.0
"""The `rayspec` Typer app.

Commands live in :mod:`rayspec.cli.commands` (auto-discovered) and, for installed third-party
packages, in the ``rayspec.cli_plugins`` entry-point group (see :mod:`rayspec.cli.plugins`).
Builtins are registered first and always win a name collision.

The root group is
:class:`~rayspec.cli.commands._loader_common.ErrorBoundaryGroup`: click invokes every command
from inside it, so that is where a :class:`~rayspec.errors.RayspecError` or an :class:`OSError`
no command expected becomes ``error: …`` on stderr with exit 2 instead of a traceback.
"""

from __future__ import annotations

import importlib
import pkgutil

import typer

from rayspec.cli import commands as _commands_pkg
from rayspec.cli.commands._loader_common import ErrorBoundaryGroup
from rayspec.cli.plugins import register_cli_plugins


def discovered_command_modules() -> list[str]:
    """Every builtin command module (``rayspec.cli.commands.<name>``), in registration order."""
    return sorted(
        f"{_commands_pkg.__name__}.{info.name}"
        for info in pkgutil.iter_modules(_commands_pkg.__path__)
        if not info.name.startswith("_")
    )


def build_app(*, plugins: bool = True) -> typer.Typer:
    """A fresh app with the builtin commands registered, then the installed CLI plugins.

    Tests build their own app (a plugin installed after import time is only visible to a new
    app); the module-level :data:`app` is the one the console script runs. ``plugins=False``
    stops before :func:`register_cli_plugins`, so a caller can reason about the *builtin*
    surface alone — what this repository ships and documents, whatever is installed next to it.
    """
    app = typer.Typer(
        name="rayspec",
        help="Declarative agent workflows for coding agents (Claude Agent SDK + Codex SDK).",
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode="rich",
        cls=ErrorBoundaryGroup,
    )

    @app.callback()
    def _root() -> None:
        """Declarative agent workflows for coding agents."""

    for module_name in discovered_command_modules():
        module = importlib.import_module(module_name)
        register = getattr(module, "register", None)
        if callable(register):
            register(app)
    if plugins:
        register_cli_plugins(app)
    return app


app = build_app()


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
