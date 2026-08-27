# SPDX-License-Identifier: Apache-2.0
"""PRD-07, R2/R4: one liveness assessment for the engine and the CLI.

The heartbeat is a fixed-interval timer independent of any step's length — a long provider
call never stalls it — so a stale beat means the process is dead, suspended or wedged, and the
reader never has to guess the writer's interval (the defect the derived interval produced).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rayspec.engine import liveness
from rayspec.engine.liveness import (
    DEAD_LIKE,
    HEARTBEAT_INTERVAL_S,
    HEARTBEAT_STALE_AFTER_S,
    Liveness,
    assess,
    heartbeat_is_stale,
)
from rayspec.schema import RunStatus
from rayspec.store.model import RunRecord

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


def record(**kwargs: object) -> RunRecord:
    base: dict[str, object] = {
        "run_id": "20260827-120000-abcd",
        "workflow_name": "w",
        "workflow_path": "w.yaml",
        "workflow_hash": "a" * 64,
        "project_slug": "local/x",
        "project_root": "/x",
        "status": RunStatus.RUNNING,
        "pid": 4242,
        "host": "this-host",
        "pid_started_at": "T0",
        "heartbeat_at": NOW - timedelta(seconds=5),
    }
    base.update(kwargs)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_constants_are_fixed_not_derived() -> None:
    assert HEARTBEAT_INTERVAL_S == 10.0
    assert HEARTBEAT_STALE_AFTER_S == 90.0
    assert HEARTBEAT_STALE_AFTER_S > HEARTBEAT_INTERVAL_S * 3
    assert not hasattr(liveness, "heartbeat_interval_s")  # nothing takes a timeout any more


def test_none_heartbeat_is_never_stale() -> None:
    assert heartbeat_is_stale(None, now=NOW) is False


@pytest.mark.parametrize(
    ("age_s", "stale"), [(0, False), (89, False), (90, False), (91, True), (3600, True)]
)
def test_staleness_threshold(age_s: int, stale: bool) -> None:
    assert heartbeat_is_stale(NOW - timedelta(seconds=age_s), now=NOW) is stale


def test_naive_stamps_are_read_as_utc() -> None:
    naive = (NOW - timedelta(seconds=200)).replace(tzinfo=None)
    assert heartbeat_is_stale(naive, now=NOW) is True


def test_assess_not_running() -> None:
    run = record(status=RunStatus.SUCCEEDED)
    assert assess(run, now=NOW, hostname="this-host") is Liveness.NOT_RUNNING


def test_assess_other_host_is_left_alone() -> None:
    run = record(host="elsewhere")
    assert assess(run, now=NOW, hostname="this-host") is Liveness.OTHER_HOST


def test_assess_dead_pid() -> None:
    run = record()
    verdict = assess(
        run,
        now=NOW,
        hostname="this-host",
        pid_exists=lambda pid: False,
        start_time_of=lambda p: None,
    )
    assert verdict is Liveness.DEAD_PID


def test_assess_missing_pid_is_dead() -> None:
    run = record(pid=None)
    assert (
        assess(run, now=NOW, hostname="this-host", pid_exists=lambda pid: True) is Liveness.DEAD_PID
    )


def test_assess_pid_reused_by_start_time() -> None:
    run = record(pid_started_at="T0")
    verdict = assess(
        run,
        now=NOW,
        hostname="this-host",
        pid_exists=lambda pid: True,
        start_time_of=lambda p: "T1",
    )
    assert verdict is Liveness.PID_REUSED


def test_assess_stale_heartbeat_with_a_live_verified_pid() -> None:
    run = record(heartbeat_at=NOW - timedelta(seconds=600))
    verdict = assess(
        run,
        now=NOW,
        hostname="this-host",
        pid_exists=lambda pid: True,
        start_time_of=lambda p: "T0",
    )
    assert verdict is Liveness.STALE_HEARTBEAT


def test_assess_alive() -> None:
    run = record()
    verdict = assess(
        run,
        now=NOW,
        hostname="this-host",
        pid_exists=lambda pid: True,
        start_time_of=lambda p: "T0",
    )
    assert verdict is Liveness.ALIVE


def test_assess_unverifiable_start_time_falls_back_to_the_heartbeat() -> None:
    """An older record with no ``pid_started_at`` cannot detect reuse: a live pid with a fresh
    beat is alive, with a stale beat it is the heartbeat that decides."""
    fresh = record(pid_started_at=None)
    stale = record(pid_started_at=None, heartbeat_at=NOW - timedelta(seconds=600))
    kwargs = {"now": NOW, "hostname": "this-host", "pid_exists": lambda pid: True}
    assert assess(fresh, start_time_of=lambda p: "whatever", **kwargs) is Liveness.ALIVE
    assert assess(stale, start_time_of=lambda p: "whatever", **kwargs) is Liveness.STALE_HEARTBEAT


def test_dead_like_is_the_set_reconcile_and_resume_act_on() -> None:
    assert (
        frozenset({Liveness.DEAD_PID, Liveness.PID_REUSED, Liveness.STALE_HEARTBEAT}) == DEAD_LIKE
    )
    assert Liveness.ALIVE not in DEAD_LIKE and Liveness.OTHER_HOST not in DEAD_LIKE
