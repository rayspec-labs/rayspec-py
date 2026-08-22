# SPDX-License-Identifier: Apache-2.0
"""Packaged data: the one recursive walk over an ``importlib.resources`` tree.

Module boundary: a single helper over :class:`~importlib.resources.abc.Traversable`, with no
rayspec imports at all. rayspec ships three trees of data files — the coding-agent skill
(:mod:`rayspec.skill`), the ``rayspec init`` scaffolds and the packaged example corpus
(:mod:`rayspec.cli.commands.init`) — and each of them needs the same listing with a different
idea of what to skip. The walk is here; what to skip stays with the tree that knows.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.resources.abc import Traversable

#: ``(relative posix path, last segment)`` of one entry — what the predicates are asked about.
Predicate = Callable[[str, str], bool]


def walk_files(
    node: Traversable,
    *,
    keep_dir: Predicate | None = None,
    keep_file: Predicate | None = None,
) -> list[tuple[str, Traversable]]:
    """``[(relative posix path, file)]`` for every file below ``node``, sorted by path.

    ``keep_dir(rel, name)`` decides whether to descend into a directory and ``keep_file(rel,
    name)`` whether to keep a file, where ``rel`` is the entry's path relative to the walk's
    root and ``name`` its last segment. Both default to keeping everything; a walk with no
    predicates lists the tree as it is.

    The recursion's own prefix is deliberately not a parameter: it is what makes ``rel``
    relative to the root, so a caller able to set it could only make that promise false.
    """
    found: list[tuple[str, Traversable]] = []
    _walk(node, "", keep_dir, keep_file, found)
    return sorted(found, key=lambda item: item[0])


def _walk(
    node: Traversable,
    prefix: str,
    keep_dir: Predicate | None,
    keep_file: Predicate | None,
    found: list[tuple[str, Traversable]],
) -> None:
    """One level of :func:`walk_files`, appending the files it keeps to ``found``."""
    for child in node.iterdir():
        rel = f"{prefix}{child.name}"
        if child.is_dir():
            if keep_dir is None or keep_dir(rel, child.name):
                _walk(child, f"{rel}/", keep_dir, keep_file, found)
        elif keep_file is None or keep_file(rel, child.name):
            found.append((rel, child))


__all__ = ["Predicate", "walk_files"]
