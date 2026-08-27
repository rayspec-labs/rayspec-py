# SPDX-License-Identifier: Apache-2.0
"""PRD-07 D7: `rayspec logs --follow` must survive the store middle-truncating a file it is
tailing — without the fix, the stale byte offset points past the shorter file's end and the
follower goes silent for the rest of the run."""

from __future__ import annotations

from pathlib import Path

from rayspec.cli.commands.logs import LogTailer
from rayspec.events.model import EventType, RunEvent
from rayspec.store.file import FileRunStore
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


def test_follow_is_not_silenced_by_a_truncation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _record("20260827-150000-sh")
    store.create(run)
    tailer = LogTailer(store, run.run_id)
    # a few events, poll them (the follower is now caught up)
    for n in range(5):
        store.append_event(run.run_id, _event(n))
    first = tailer.poll()
    ns = [item.data["n"] for _s, item in first if item.data.get("n") is not None]
    assert ns == [0, 1, 2, 3, 4]
    # now flood past 2x the cap so the store truncates events.jsonl (new inode), then a few more
    for n in range(5, 400):
        store.append_event(run.run_id, _event(n))
    for n in range(400, 410):
        store.append_event(run.run_id, _event(n))
    # the follower must still deliver the LATEST events and see the truncation marker — not go blank
    more = tailer.poll()
    assert more, "the follower went silent after the truncation"
    delivered = [item.data.get("n") for _s, item in more]
    assert 409 in delivered, "the newest events after a truncation must still arrive"
    assert any(item.data.get("log_truncated") for _s, item in more), "the marker is surfaced"
    # and a further append still flows
    store.append_event(run.run_id, _event(410))
    assert 410 in [item.data.get("n") for _s, item in tailer.poll()]


def test_follow_delivers_the_marker_and_tail_when_the_last_shown_line_is_dropped(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    run = _record("20260827-150100-drop")
    store.create(run)
    tailer = LogTailer(store, run.run_id)
    store.append_event(run.run_id, _event(0))
    tailer.poll()  # last shown line = event 0, which the flood below will drop from the middle
    for n in range(1, 400):
        store.append_event(run.run_id, _event(n))
    out = tailer.poll()
    assert out
    assert any(item.data.get("log_truncated") for _s, item in out)
    assert 399 in [item.data.get("n") for _s, item in out]
