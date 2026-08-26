# SPDX-License-Identifier: Apache-2.0
"""PRD-07 (detached runs), R2: a periodically-refreshed heartbeat timestamp lives on
``RunRecord`` next to ``pid``/``host``/``pid_started_at`` — no new storage location, and old
``run.json`` files without it must still load (mirrors ``pid_started_at``'s own backward
compatibility, exercised in ``tests/engine/test_runner_pid.py::test_run_record_reads_without_the_field``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from rayspec.store.model import RunRecord


def _base_kwargs() -> dict:
    return {
        "run_id": "20260827-100000-hbt1",
        "workflow_name": "w",
        "workflow_path": "w.yaml",
        "workflow_hash": "a" * 64,
        "project_slug": "local/x",
        "project_root": "/x",
    }


def test_run_record_gains_heartbeat_field() -> None:
    """``RunRecord`` exposes a heartbeat timestamp field, settable like any other timestamp."""
    now = datetime.now(UTC)
    run = RunRecord(pid=4242, host="here", heartbeat_at=now, **_base_kwargs())
    assert run.heartbeat_at == now


def test_run_record_heartbeat_defaults_to_none_on_a_fresh_record() -> None:
    run = RunRecord(**_base_kwargs())
    assert run.heartbeat_at is None


def test_old_run_json_without_heartbeat_loads_with_none() -> None:
    """A ``run.json`` written before this field existed has no ``heartbeat_at`` key at all."""
    run = RunRecord.model_validate(
        {
            "schema": 1,
            "run_id": "20260827-100000-old1",
            "workflow_name": "w",
            "workflow_path": "w.yaml",
            "workflow_hash": "a" * 64,
            "project_slug": "local/x",
            "project_root": "/x",
            "pid": 4242,
        }
    )
    assert run.pid == 4242
    assert run.heartbeat_at is None


def test_heartbeat_round_trips_through_json() -> None:
    now = datetime.now(UTC)
    run = RunRecord(pid=1, host="h", heartbeat_at=now, **_base_kwargs())
    dumped = run.model_dump(mode="json", by_alias=True)
    assert "heartbeat_at" in dumped
    restored = RunRecord.model_validate(dumped)
    assert restored.heartbeat_at == now
