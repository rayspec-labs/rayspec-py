# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R2: heartbeat pacing, derived from the step timeout rather than fixed.

One helper, so ``rayspec.engine.runner`` (which writes the heartbeat) and
``rayspec.cli._runs_common`` (which reads it back to decide whether a ``running`` record is
still believable) never drift on the numbers. Per the plan-gate answer: the interval is
``min(60s, max(5s, step_timeout / 10))`` and a heartbeat is considered stale once it has not
moved for ``3x`` that interval.
"""

from __future__ import annotations

#: Floor and ceiling of the heartbeat interval, in seconds.
MIN_INTERVAL_S = 5.0
MAX_INTERVAL_S = 60.0
#: How many missed intervals make a heartbeat stale.
STALE_MULTIPLIER = 3
#: Fallback base when the workflow states no ``defaults.timeout`` to derive an interval from.
DEFAULT_BASE_TIMEOUT_S = 60.0


def heartbeat_interval_s(step_timeout: float | None = None) -> float:
    """The heartbeat interval for a run whose per-step timeout is ``step_timeout``.

    ``None``/``0`` (no ``defaults.timeout``) falls back to :data:`DEFAULT_BASE_TIMEOUT_S` — a
    run with no per-step cap still gets a bounded, reasonable pace rather than an unbounded one.
    """
    base = step_timeout if step_timeout else DEFAULT_BASE_TIMEOUT_S
    return min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, base / 10))


def heartbeat_stale_after_s(step_timeout: float | None = None) -> float:
    """Seconds since the last heartbeat after which a ``running`` record is no longer believable."""
    return heartbeat_interval_s(step_timeout) * STALE_MULTIPLIER


__all__ = [
    "DEFAULT_BASE_TIMEOUT_S",
    "MAX_INTERVAL_S",
    "MIN_INTERVAL_S",
    "STALE_MULTIPLIER",
    "heartbeat_interval_s",
    "heartbeat_stale_after_s",
]
