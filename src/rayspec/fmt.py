# SPDX-License-Identifier: Apache-2.0
"""How rayspec renders a duration — the one place, for every surface.

Module boundary: pure functions over numbers, no rayspec imports at all. Two renderings live
here because rayspec deliberately has two, and keeping them side by side is what stops a third
appearing:

* :func:`format_duration` — the compact column form (``12.3s``, ``1m35s``) used by the run
  listings and the live console tree, where the figure shares a line with a status and a cost;
* :func:`humanize_duration` — the spaced prose form (``31m 52s``, ``1h 3m``) used by the
  approval panel and the run-level cap reasons, where it is read as a sentence.

Token counts and costs are *not* here: :mod:`rayspec.providers.pricing` owns their rendering
(``format_tokens``, ``format_cost``, ``cost_marker``) because it owns the numbers.
"""

from __future__ import annotations


def format_duration(ms: float | None) -> str:
    """``850ms`` · ``12.3s`` · ``1m35s`` · ``1h02m``; ``-`` when unknown."""
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{int(ms)}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def humanize_duration(ms: float | None) -> str:
    """``0.3s`` · ``1.2s`` · ``31m 52s`` · ``1h 3m``; ``—`` when unknown."""
    if ms is None:
        return "—"
    seconds = max(0.0, float(ms)) / 1000.0
    if seconds < 59.95:  # below the point where ``.1f`` would print ``60.0s``
        return f"{seconds:.1f}s"
    minutes, sec = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


__all__ = ["format_duration", "humanize_duration"]
