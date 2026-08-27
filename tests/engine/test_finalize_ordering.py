# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R3: `logs --follow` keys its final drain off run.json's status and polls events.jsonl
exactly once more after it flips terminal. So the closing `run.finished` event MUST already be on
disk when run.json goes terminal — otherwise a follower can see the run end and drain the event
log before the closing line was written, terminating without ever printing it. This pins the
ordering invariant in `_finalize` (emit before the terminal save)."""

from __future__ import annotations

import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.runner import Runner
from rayspec.store.file import EVENTS_JSONL

from .conftest import FakeLeaf, Harness

pytestmark = pytest.mark.anyio


async def test_run_finished_is_on_disk_before_run_json_goes_terminal(harness: Harness) -> None:
    harness.workflow("t", "rayspec: 1\nname: t\nsteps:\n  - {id: a, shell: ok}\n")
    observed: dict[str, bool] = {}
    real_save = harness.store.save

    def watching_save(run, *args, **kwargs):
        if run.status.is_terminal and "at_terminal_save" not in observed:
            events = harness.store.run_dir(run.run_id) / EVENTS_JSONL
            text = events.read_text(encoding="utf-8") if events.exists() else ""
            observed["at_terminal_save"] = "run.finished" in text
        return real_save(run, *args, **kwargs)

    harness.store.save = watching_save  # type: ignore[method-assign]
    leaf = FakeLeaf()
    runner = Runner(
        harness.load("t"),
        inputs={},
        store=harness.store,
        sinks=harness.sink,
        project_root=harness.root,
        project_slug="local/test",
        options=RunOptions(interactive=False),
        executors={"shell": leaf, "python": leaf},
        engine=harness.engine,
    )
    result = await runner.run()
    assert result.status.value == "succeeded", result.status
    assert observed.get("at_terminal_save") is True, (
        "run.json reached a terminal status before run.finished was written to events.jsonl — "
        "a follower can drain and exit without the closing line"
    )
