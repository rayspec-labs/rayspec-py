# SPDX-License-Identifier: Apache-2.0
"""PRD-07, R2: process metadata (PID + heartbeat) in the run directory.

The heartbeat must (a) live inside the existing ``run.json`` — no new file/format — and (b) be
refreshed periodically while a step is in flight, not written once at run start. Neither is
implemented yet: today ``run.json`` never gains a ``heartbeat_at`` key at all.
"""

from __future__ import annotations

import json

import anyio
import pytest

from rayspec.engine import liveness
from rayspec.engine.context import RunOptions
from rayspec.providers.stub import StubProvider
from rayspec.schema import RunStatus
from rayspec.store.model import new_run_id

from .conftest import Harness

pytestmark = pytest.mark.anyio


def wf(steps: str) -> str:
    return f"rayspec: 1\nname: t\nsteps:\n{steps}"


async def test_heartbeat_is_updated_periodically_during_a_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single step slower than one heartbeat interval must see the heartbeat advance more
    than once while it runs — a timer, not a one-shot stamp at start."""
    monkeypatch.setattr(
        liveness, "HEARTBEAT_INTERVAL_S", 0.5
    )  # the timer is fixed; shorten it here
    harness.workflow(
        "t",
        wf("""
  - id: slow
    agent: {provider: stub}
    prompt: "go"
"""),
    )
    run_id = new_run_id()
    provider = StubProvider(script={"defaults": {"latency_ms": 6500}})
    samples: list[object] = []
    done = anyio.Event()

    async def poll() -> None:
        while not done.is_set():
            try:
                rec = harness.store.load(run_id)
            except Exception:  # run.json may not exist for an instant at start
                await anyio.sleep(0.2)
                continue
            samples.append(getattr(rec, "heartbeat_at", "MISSING"))
            await anyio.sleep(0.4)

    async with anyio.create_task_group() as tg:
        tg.start_soon(poll)
        result = await harness.run(
            "t",
            options=RunOptions(interactive=False),
            providers={"stub": provider},
            run_id=run_id,
        )
        done.set()

    assert result.status is RunStatus.SUCCEEDED
    assert "MISSING" not in samples, "RunRecord has no heartbeat_at field yet"
    distinct = {s for s in samples if s is not None}
    assert len(distinct) >= 2, (
        f"heartbeat_at did not advance more than once during a {6.5}s step: {samples}"
    )


async def test_heartbeat_uses_no_new_storage_location(harness: Harness) -> None:
    """The heartbeat is a field inside ``run.json`` — not a sibling file, not a new format."""
    harness.workflow("t", wf("  - {id: a, shell: echo built}\n"))
    result = await harness.run("t", options=RunOptions(interactive=False))
    assert result.status is RunStatus.SUCCEEDED
    run_dir = harness.store.run_dir(result.run_id)
    before = sorted(p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*") if p.is_file())
    raw = json.loads((run_dir / "run.json").read_text())
    assert "heartbeat_at" in raw, "heartbeat must be a field of run.json, not a new artefact"
    after = sorted(p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*") if p.is_file())
    assert after == before, "no new file should appear for the heartbeat"
