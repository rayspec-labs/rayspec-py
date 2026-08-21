# SPDX-License-Identifier: Apache-2.0
"""Pure lint helpers usable by the loader without building an engine.

- :func:`has_braces` — ``when:``/``until:``/``each:`` are *bare expressions*; ``{{``/``{%``
  inside them is almost always a mistake (``when: "{{ x }}"`` renders text, not a bool).
- :func:`has_gha_syntax` — ``${{`` in a ``shell:`` body is GitHub-Actions syntax; rayspec
  renders ``{{ x }}`` to ``${RAYSPEC_V<n>}`` so ``"${{ x }}"`` would produce ``"$${RAYSPEC_V1}"``.

Literal braces: wrap Go-template-style text (``docker --format '{{.ID}}'``, ``gh --json -q``,
``kubectl -o go-template``, ``helm``) and ``printf '{{'`` in ``{% raw %} ... {% endraw %}``.
Code-body environments use ``{{# ... #}}`` as comment delimiters so bash ``${#VAR}`` survives.

Module boundary: no rayspec imports.
"""

from __future__ import annotations


def has_braces(expression: str) -> bool:
    """True when an *expression field* contains template delimiters (``{{`` or ``{%``)."""
    return "{{" in expression or "{%" in expression


def has_gha_syntax(shell_body: str) -> bool:
    """True when a shell body contains GitHub-Actions style ``${{``."""
    return "${{" in shell_body


__all__ = ["has_braces", "has_gha_syntax"]
