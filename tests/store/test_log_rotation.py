# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R8: per-file log caps with hysteresis and a marker at the cut.

A file is trimmed only once it grows past ``2x`` the cap, back to the cap — so a noisy stream is
not rewritten on every append (the storm the flat "> cap" trigger caused). The cut leaves a
parseable marker; ``events.jsonl``, each step's ``stream.jsonl`` and ``audit.jsonl`` are all
capped. Tests use a small cap on the store instance (``log_cap_bytes``) rather than 16 MiB.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.store.file import AUDIT_JSONL, EVENTS_JSONL, STREAM_JSONL, FileRunStore
from rayspec.store.model import RunRecord

CAP = 4096


def _store(tmp_path: Path) -> FileRunStore:
    store = FileRunStore(tmp_path / "store")
    store.log_cap_bytes = CAP
    return store


def _record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="w",
        workflow_path="w.yaml",
        workflow_hash="a" * 64,
        project_slug="local/x",
        project_root="/x",
    )


def _event(n: int) -> RunEvent:
    return RunEvent(type=EventType.WARNING, run_id="r", data={"n": n, "pad": "x" * 200})


def test_events_are_capped_with_2x_hysteresis(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _record("20260827-140000-ev")
    store.create(run)
    path = store.run_dir(run.run_id) / EVENTS_JSONL
    for n in range(400):  # ~90 KiB of events
        store.append_event(run.run_id, _event(n))
        assert path.stat().st_size <= CAP * 2, n  # never past the trigger (hysteresis bound)
    # it was trimmed at least once (a marker is present), and it never exceeded 2x the cap
    assert any(
        json.loads(line).get("data", {}).get("log_truncated")
        for line in path.read_text().splitlines()
    )


def test_no_rewrite_below_the_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    run = _record("20260827-140100-few")
    store.create(run)
    from rayspec.store import file as file_mod

    calls = {"n": 0}
    real = file_mod.truncate_path
    monkeypatch.setattr(
        file_mod,
        "truncate_path",
        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real(*a, **k))[1],
    )
    for n in range(400):
        store.append_event(run.run_id, _event(n))
    # ~90 KiB / 4 KiB cap with a 2x trigger ⇒ a handful of trims, not one per append
    assert calls["n"] <= 40, calls["n"]  # ~23 expected; without hysteresis it would be ~380


def test_events_marker_is_a_parseable_warning_with_log_truncated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _record("20260827-140200-mk")
    store.create(run)
    for n in range(400):
        store.append_event(run.run_id, _event(n))
    path = store.run_dir(run.run_id) / EVENTS_JSONL
    markers = []
    for line in path.read_text().splitlines():
        obj = json.loads(line)  # every surviving line parses
        if obj.get("data", {}).get("log_truncated"):
            markers.append(obj)
    assert markers, "a truncation marker event must be present"
    assert markers[-1]["data"]["log_truncated"]["dropped_bytes"] > 0


def test_stream_marker_is_a_parseable_warning_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _record("20260827-140300-sm")
    store.create(run)
    for _n in range(400):
        store.append_stream(
            run.run_id, "step", StreamRecord(kind="stdout", text="row " + "y" * 200)
        )
    store.flush_streams(run.run_id, "step")
    path = store.step_dir(run.run_id, "step") / STREAM_JSONL
    kinds = [json.loads(line)["kind"] for line in path.read_text().splitlines()]
    assert "warning" in kinds
    assert path.stat().st_size <= CAP * 2


def test_audit_jsonl_is_capped(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "store", audit=True)
    store.log_cap_bytes = CAP
    run = _record("20260827-140400-au")
    store.create(run)
    for n in range(600):
        store.append_event(run.run_id, _event(n))
    path = store.run_dir(run.run_id) / AUDIT_JSONL
    assert path.exists() and path.stat().st_size <= CAP * 2
    for line in path.read_text().splitlines():
        json.loads(line)  # every surviving audit line parses
