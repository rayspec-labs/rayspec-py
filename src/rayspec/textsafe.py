# SPDX-License-Identifier: Apache-2.0
"""Neutralise untrusted text before it reaches a terminal.

Module boundary: pure string functions, no rayspec imports. Every renderer of agent/step/input/
output text (console sink, approval panel, ``show``, ``logs``, run summary) passes the text
through :func:`safe_text` (plain text for ``rich.text.Text`` cells) or :func:`safe_markup`
(when the string is interpolated into a Rich markup string).

What is removed: C0 control characters except ``\\n``/``\\t`` (``\\r`` included), DEL, C1
controls (8-bit CSI/OSC included), and every 7-bit escape sequence — CSI (``ESC [ … final``:
colours, cursor moves, clear screen), OSC/DCS/SOS/PM/APC strings terminated by BEL or ST
(``ESC \\``: title changes, hyperlinks, clipboard writes), SS3, nF/Fp/Fe/Fs two-byte escapes
and a lone ESC. An *unterminated* OSC loses only its introducer so the payload stays visible as
plain text instead of swallowing the rest of the output. Printable Unicode is kept; Rich markup
(``[bold]``) is left as literal text by :func:`safe_text` and escaped by :func:`safe_markup`.
Signature pinned by CONTRACTS.md: ``safe_text(s: str, *, keep_newlines: bool = True) -> str``
(``None``/non-str are tolerated and ``str()``-ed); ``safe_markup(s)`` likewise.
"""

from __future__ import annotations

import re
from typing import Any

from rich.markup import escape as _rich_escape

#: Every escape sequence / control character that must never reach the terminal, tried in order.
_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[\]PX^_][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"  # OSC/DCS/SOS/PM/APC, terminated
    r"|\x1bO[@-~]"  # SS3
    r"|\x1b[ -/]+[0-~]"  # nF (with intermediates)
    r"|\x1b[0-~]"  # Fp / Fe / Fs two-byte escapes (an unterminated OSC loses ``ESC ]`` only)
    r"|\x1b"  # a lone ESC
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
    r"|[\x9d\x90\x98\x9e\x9f][^\x07\x9c\x1b]*(?:\x07|\x9c|\x1b\\)?"  # 8-bit strings
    r"|[\x80-\x9f]"  # other C1 controls
    r"|[\x00-\x08\x0b-\x1f\x7f]"  # C0 controls except TAB/LF, plus DEL
)
_NEWLINES_RE = re.compile(r"[\n\t]")


def safe_text(s: Any, *, keep_newlines: bool = True) -> str:
    """Return ``s`` as plain text with every control character / escape sequence removed.

    ``keep_newlines=True`` keeps ``\\n`` and ``\\t`` (multi-line panels); ``False`` turns them
    into single spaces (one-row tree lines, table cells). ``None`` becomes ``''``; any other
    non-string is ``str()``-ed first. Idempotent.
    """
    if s is None:
        return ""
    text = s if isinstance(s, str) else str(s)
    cleaned = _ESCAPE_RE.sub("", text)
    if not keep_newlines:
        cleaned = _NEWLINES_RE.sub(" ", cleaned)
    return cleaned


def safe_markup(s: Any, *, keep_newlines: bool = True) -> str:
    """:func:`safe_text` + ``rich.markup.escape`` — for interpolation into a markup string."""
    return _rich_escape(safe_text(s, keep_newlines=keep_newlines))


__all__ = ["safe_markup", "safe_text"]
