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

    result = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(poll)
        result = await harness.run(
            "t",
            options=RunOptions(interactive=False),
            providers={"stub": provider},
            run_id=run_id,
        )
        done.set()

    assert result is not None
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


async def test_a_heartbeat_write_failure_never_ends_the_run(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD-07 R2 (review N1): an OSError from the periodic liveness save must not escape the
    heartbeat task, cancel the graph and bypass finalize — the run still reaches a terminal
    status with its pid cleared, and the step's own saves keep proving liveness."""
    from rayspec.engine.context import RunContext

    monkeypatch.setattr(liveness, "HEARTBEAT_INTERVAL_S", 0.3)
    harness.workflow("t", wf("  - {id: slow, agent: {provider: stub}, prompt: go}\n"))
    provider = StubProvider(script={"defaults": {"latency_ms": 1500}})

    async def boom(self: RunContext) -> None:  # only the periodic timer calls touch_heartbeat now
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(RunContext, "touch_heartbeat", boom)
    result = await harness.run(
        "t", options=RunOptions(interactive=False), providers={"stub": provider}
    )
    assert result.status is RunStatus.SUCCEEDED, result.reason
    stored = harness.store.load(result.run_id)
    assert stored.status is RunStatus.SUCCEEDED
    assert stored.pid is None
    assert all(r.status is not RunStatus.RUNNING for r in stored.steps.values())


async def test_a_finished_step_advances_the_heartbeat(harness: Harness) -> None:
    """Every engine-originated save stamps ``heartbeat_at`` (``_stamp_alive``), so a step
    finishing moves it even with the timer never firing — a write is proof of life."""
    harness.workflow(
        "t", wf("  - {id: a, shell: echo one}\n  - {id: b, needs: [a], shell: echo two}\n")
    )
    result = await harness.run("t", options=RunOptions(interactive=False))
    assert result.status is RunStatus.SUCCEEDED
    a, b = result.steps["a"], result.steps["b"]
    assert a.ended_at is not None and b.ended_at is not None
    stored = json.loads((harness.store.run_dir(result.run_id) / "run.json").read_text())
    assert stored["heartbeat_at"] is not None


async def test_the_prompt_executor_does_not_stamp_the_heartbeat_itself(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two per-call ``touch_heartbeat`` writes are gone: liveness during a prompt step rests
    on the periodic timer and the step's own record save, not two extra run.json writes."""
    import inspect

    from rayspec.engine.executors import prompt as prompt_exec

    # the real regression is the executor calling touch_heartbeat around the provider call;
    # assert the module simply does not mention it (the old +2-per-prompt writes are gone)
    assert "touch_heartbeat" not in inspect.getsource(prompt_exec)
    monkeypatch.setattr(liveness, "HEARTBEAT_INTERVAL_S", 3600)  # timer effectively off
    harness.workflow("t", wf("  - {id: p, agent: {provider: stub}, prompt: go}\n"))
    saves = {"n": 0}
    real = harness.store.save

    def count(run: object) -> None:
        saves["n"] += 1
        real(run)  # type: ignore[arg-type]

    monkeypatch.setattr(harness.store, "save", count)
    result = await harness.run(
        "t", options=RunOptions(interactive=False), providers={"stub": StubProvider()}
    )
    assert result.status is RunStatus.SUCCEEDED
    # measured baseline is 6 saves (start, toolchain, step-start, step-finish, finalize + the
    # record's own persist); the reverted "+2 touch_heartbeat per prompt" would be 8, so a bound
    # at the baseline catches that regression rather than waving it through
    assert saves["n"] <= 6, saves["n"]
