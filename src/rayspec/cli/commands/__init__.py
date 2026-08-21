# SPDX-License-Identifier: Apache-2.0
"""CLI command modules. Each module exposes ``register(app: typer.Typer) -> None``.

Modules are auto-discovered by :mod:`rayspec.cli.app`, so adding a command never requires editing
``app.py`` (keeps parallel work conflict-free).
"""
