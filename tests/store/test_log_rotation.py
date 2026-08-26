# SPDX-License-Identifier: Apache-2.0
"""PRD-07, R8: log rotation bound — a background run nobody watches must not fill the disk.

Per the plan-gate answer: a fixed 16 MiB cap per stream file (``events.jsonl`` and each step's
``stream.jsonl``), truncating whole lines from the middle (keep roughly the first third and the
last two thirds), never tearing a JSON line. None of this exists yet: ``FileRunStore.append_stream``
(and ``append_event``) just keep appending — there is no cap, no truncation, anywhere.
"""

from __future__ import annotations

from pathlib import Path

from rayspec.events.model import StreamRecord
from rayspec.store.file import STREAM_JSONL, FileRunStore
from rayspec.store.model import RunRecord

CAP_BYTES = 16 * 1024 * 1024  # 16 MiB, the fixed default from the plan-gate answer
_FILLER = "x" * 200_000  # large chunks: few append calls needed to cross the cap


def _record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="w",
        workflow_path="w.yaml",
        workflow_hash="a" * 64,
        project_slug="local/x",
        project_root="/x",
    )


def _pad_stream_past(store: FileRunStore, run_id: str, step: str, target_bytes: int) -> None:
    path = store.step_dir(run_id, step) / STREAM_JSONL
    n = 0
    while not path.exists() or path.stat().st_size < target_bytes:
        store.append_stream(run_id, step, StreamRecord(kind="text", text=f"{_FILLER}-{n}"))
        n += 1


def test_log_size_capped_with_middle_truncation(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "store")
    run = _record("20260827-120000-cap1")
    store.create(run)
    head_text = "HEAD-MARKER-0000"
    tail_text = "TAIL-MARKER-9999"
    store.append_stream(run.run_id, "noisy", StreamRecord(kind="text", text=head_text))
    _pad_stream_past(store, run.run_id, "noisy", CAP_BYTES * 2)
    store.append_stream(run.run_id, "noisy", StreamRecord(kind="text", text=tail_text))

    stream_path = store.step_dir(run.run_id, "noisy") / STREAM_JSONL
    size = stream_path.stat().st_size
    assert size <= CAP_BYTES, f"stream.jsonl grew to {size} bytes, past the {CAP_BYTES} cap"

    content = stream_path.read_text()
    assert head_text in content, "the head of the stream must survive truncation"
    assert tail_text in content, "the tail of the stream must survive truncation"


def test_log_rotation_does_not_corrupt_jsonl_framing(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "store")
    run = _record("20260827-120100-cap2")
    store.create(run)
    _pad_stream_past(store, run.run_id, "noisy", CAP_BYTES * 2)

    stream_path = store.step_dir(run.run_id, "noisy") / STREAM_JSONL
    size = stream_path.stat().st_size
    # asserted first and on purpose: today nothing truncates, so nothing tears a line either —
    # the cap itself is the feature under test; framing is only meaningful once it exists.
    assert size <= CAP_BYTES, f"stream.jsonl was never capped ({size} bytes)"
    for line in stream_path.read_text().splitlines():
        if line.strip():
            StreamRecord.from_json(line)  # every surviving line must still parse


def test_unwatched_detached_run_disk_use_bounded(tmp_path: Path) -> None:
    """A long, noisy run with several streaming steps must not grow past a small multiple of
    the per-file cap, even though nobody is following it (no truncation is ever triggered by a
    reader — the store itself must bound what it writes)."""
    store = FileRunStore(tmp_path / "store")
    run = _record("20260827-120200-cap3")
    store.create(run)
    steps = ["step0", "step1", "step2", "step3"]
    for step in steps:
        _pad_stream_past(store, run.run_id, step, int(CAP_BYTES * 1.5))

    run_dir = store.run_dir(run.run_id)
    total = sum(p.stat().st_size for p in run_dir.rglob("*") if p.is_file())
    budget = CAP_BYTES * (len(steps) + 1) * 1.1  # +1 slot for events.jsonl, 10% slack
    assert total <= budget, f"unwatched run directory grew to {total} bytes (budget {budget:.0f})"
