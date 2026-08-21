# SPDX-License-Identifier: Apache-2.0
"""Packaged scaffold templates for ``rayspec init`` (one directory per ``--kind``).

Boundary: data only. ``rayspec.cli.commands.init`` copies these trees into ``<root>/.rayspec/``
via :mod:`importlib.resources`; nothing else imports this package.
"""
