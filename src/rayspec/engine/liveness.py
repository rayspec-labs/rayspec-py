# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R2/R4: liveness — the heartbeat's numbers and the one assessment of a stored run.

The heartbeat is a **fixed-interval** timer (:data:`HEARTBEAT_INTERVAL_S`) that the engine runs
for the life of a run, independent of any step's length: a long provider call never stalls it,
because the timer is its own task, so a beat that has not moved for
:data:`HEARTBEAT_STALE_AFTER_S` means the process is dead, suspended or wedged — never "busy".
Deriving the interval from the step timeout (an earlier shape) bought nothing and let the writer
and the reader disagree on the threshold; here there is one pair of numbers and nothing to guess.

:func:`assess` is the single liveness rule: the CLI's ``reconcile_run`` (``runs``/``show``/
``logs --follow``/``cancel``) and the engine's resume guard both call it, so a record that one
of them calls ``interrupted`` the other never calls ``running``. ``pid_exists`` and
``start_time_of`` are injectable for tests; the real probes are :func:`process_start_time`
(``ps -o lstart=`` under a pinned locale, else ``/proc``) and a zero-signal ``os.kill``.
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from rayspec.schema import RunStatus
from rayspec.store.model import RunRecord

#: Seconds between two beats of a live run. Fixed: the timer does not depend on the step.
HEARTBEAT_INTERVAL_S: float = 10.0
#: Seconds without a beat after which a ``running`` record is no longer believable — nine
#: missed beats, which a suspended laptop or a wedged event loop exceeds and a busy one never does.
HEARTBEAT_STALE_AFTER_S: float = 90.0


class Liveness(StrEnum):
    """What a stored record says about the process behind it, most decisive first."""

    NOT_RUNNING = "not_running"  # the record is not ``running`` — nothing to assess
    OTHER_HOST = "other_host"  # recorded on another machine: cannot be checked from here
    DEAD_PID = "dead_pid"  # no such process
    PID_REUSED = "pid_reused"  # a process exists, but it is not the one that was recorded
    STALE_HEARTBEAT = "stale_heartbeat"  # the process exists, its heartbeat stopped
    ALIVE = "alive"


#: The verdicts ``reconcile_run`` flips to ``interrupted`` and a resume may proceed over.
DEAD_LIKE: frozenset[Liveness] = frozenset(
    {Liveness.DEAD_PID, Liveness.PID_REUSED, Liveness.STALE_HEARTBEAT}
)


def _utc(moment: datetime) -> datetime:
    """``moment`` as UTC; a naive stamp is read as UTC — that is what the store writes."""
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC)


def heartbeat_is_stale(heartbeat_at: datetime | None, *, now: datetime | None = None) -> bool:
    """Whether a heartbeat has not moved for longer than :data:`HEARTBEAT_STALE_AFTER_S`.

    ``None`` (a record written before the field existed, or an engine that has not ticked yet)
    is never stale on its own — liveness then rests on the pid checks alone.
    """
    if heartbeat_at is None:
        return False
    moment = now or datetime.now(UTC)
    return (_utc(moment) - _utc(heartbeat_at)).total_seconds() > HEARTBEAT_STALE_AFTER_S


_PROC_ROOT = Path("/proc")


#: Environment for the ``ps`` probe: the ``lstart`` string depends on the locale (``Do. 20 Aug.``
#: vs ``Thu Aug 20``) and on ``TZ``, and the engine (launch) and ``rayspec cancel`` may run under
#: different shells (cron/CI vs interactive) — pin both so the two sides always agree.
_PS_ENV = {"LC_ALL": "C", "TZ": "UTC"}


def _ps_lstart(pid: int, *, timeout_s: float) -> str | None:
    """``ps -o lstart= -p <pid>`` under ``LC_ALL=C TZ=UTC``, stripped; ``None`` when the process
    is gone or ``ps`` fails (non-zero exit, e.g. a busybox ``ps`` without ``-o lstart``).

    Raises :class:`FileNotFoundError` when there is no ``ps`` at all and
    :class:`subprocess.TimeoutExpired` when it hangs (the caller falls back to ``/proc``).
    """
    proc = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
        env={**os.environ, **_PS_ENV},
    )
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip()
    return line or None


def _proc_starttime(pid: int, *, proc_root: Path | None = None) -> str | None:
    """Field 22 (``starttime``, clock ticks since boot) of ``/proc/<pid>/stat`` — Linux only.

    The ``comm`` field (2) is parenthesised and may contain spaces and parentheses, so the fields
    are counted from the LAST ``)``. ``None`` when the file is missing or malformed.
    """
    root = _PROC_ROOT if proc_root is None else proc_root
    try:
        text = (root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    _, sep, rest = text.rpartition(")")
    if not sep:
        return None
    fields = rest.split()
    # ``rest`` starts at field 3 (state): starttime is field 22 → index 19
    if len(fields) < 20 or not fields[19].isdigit():
        return None
    return fields[19]


def process_start_time(pid: int, *, timeout_s: float = 5.0) -> str | None:
    """The start time of process ``pid`` as an opaque string to compare for equality.

    The ``ps -o lstart= -p <pid>`` output run under a fixed environment (``LC_ALL=C TZ=UTC``, e.g.
    ``Thu Aug 20 12:00:00 2026``; one-second resolution) — so the string is the same for the same
    process whichever shell, locale or timezone the caller has; when ``ps`` is missing, fails
    (busybox without ``-o lstart``) or hangs, the ``/proc/<pid>/stat`` ``starttime`` field on
    Linux. ``None`` for a pid that does not exist, an invalid pid, or when neither source can be
    read — callers treat "unknown" as "not verified". The engine records it next to ``pid`` in
    ``run.json`` at launch and on resume; ``rayspec cancel`` compares it with the live process.
    """
    if pid <= 0:
        return None
    value: str | None = None
    try:
        value = _ps_lstart(pid, timeout_s=timeout_s)
    except (OSError, subprocess.SubprocessError):
        value = None
    if value is not None:
        return value
    return _proc_starttime(pid)


def pid_exists(pid: int) -> bool:
    """Whether a process with ``pid`` exists on this host (POSIX ``kill -0`` semantics)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def run_pid_alive(run: RunRecord) -> bool:
    """Whether the process recorded in ``run.json`` still exists (same host only; POSIX)."""
    if run.pid is None or (run.host and run.host != socket.gethostname()):
        return False
    return pid_exists(run.pid)


def assess(
    run: RunRecord,
    *,
    now: datetime | None = None,
    hostname: str | None = None,
    pid_exists: Callable[[int], bool] = pid_exists,
    start_time_of: Callable[[int], str | None] = process_start_time,
) -> Liveness:
    """The one liveness rule for a stored record — see the module docstring.

    Order: not running → other host → no such process → a different process behind the same
    pid (start time differs; an *unknown* live start time cannot prove reuse and falls through)
    → a stale heartbeat → alive.
    """
    if run.status is not RunStatus.RUNNING:
        return Liveness.NOT_RUNNING
    host = hostname if hostname is not None else socket.gethostname()
    if run.host and run.host != host:
        return Liveness.OTHER_HOST
    if run.pid is None or run.pid <= 0 or not pid_exists(run.pid):
        return Liveness.DEAD_PID
    if run.pid_started_at is not None:
        live = start_time_of(run.pid)
        if live is not None and live != run.pid_started_at:
            return Liveness.PID_REUSED
    if heartbeat_is_stale(run.heartbeat_at, now=now):
        return Liveness.STALE_HEARTBEAT
    return Liveness.ALIVE


__all__ = [
    "DEAD_LIKE",
    "HEARTBEAT_INTERVAL_S",
    "HEARTBEAT_STALE_AFTER_S",
    "Liveness",
    "assess",
    "heartbeat_is_stale",
    "pid_exists",
    "process_start_time",
    "run_pid_alive",
]
