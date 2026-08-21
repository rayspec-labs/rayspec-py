# SPDX-License-Identifier: Apache-2.0
"""Documentation links quoted by CLI hints.

Boundary: constants only. A ``uv tool install`` / ``uvx`` user has no checkout of the repository,
so a hint must never cite a bare repo-relative path (``docs/providers.md#pricing``); it quotes
:func:`docs_url` (a full GitHub URL) or ``rayspec <cmd> --help`` instead. Other scopes that print
hints import :func:`docs_url` from here.
"""

from __future__ import annotations

#: Where the published docs live (``main`` of the public repository).
DOCS_BASE = "https://github.com/rayspec-labs/rayspec-py/blob/main/"


def docs_url(rel: str) -> str:
    """Full URL of a repo-relative path such as ``docs/providers.md#pricing`` (anchors kept)."""
    return DOCS_BASE + rel.lstrip("/")


__all__ = ["DOCS_BASE", "docs_url"]
