"""Temporary review probes for PRD-07 — delete after use."""

from __future__ import annotations

import threading
import time

import anyio
import pytest

from rayspec.engine.cancel import write_cancel_flag
from rayspec.engine.context import RunOptions
from rayspec.providers.stub import StubProvider
from rayspec.store.model import new_run_id

from .conftest import Harness

pytestmark = pytest.mark.anyio


async def test_probe_a_heartbeat_save_failure(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rayspec.engine.runner.HEARTBEAT_INTERVAL_S", 0.5)
    harness.workflow(
        "t",
        "rayspec: 1\nname: t\nsteps:\n  - id: slow\n    agent: {provider: stub}\n    prompt: go\n",
    )
    run_id = new_run_id()
    provider = StubProvider(script={"defaults": {"latency_ms": 2500}})
    real_save = harness.store.save
    t0 = time.monotonic()
    calls: list = []

    def flaky_save(run):
        dt = time.monotonic() - t0
        if threading.current_thread() is not threading.main_thread() and 0.8 < dt < 1.6:
            calls.append(("RAISE", round(dt, 2)))
            raise OSError(28, "No space left on device")
        real_save(run)

    harness.store.save = flaky_save  # type: ignore[method-assign]
    runner = harness.runner(
        "t", options=RunOptions(interactive=False), providers={"stub": provider}, run_id=run_id
    )
    try:
        res = await runner.run()
        print("PROBE-A RETURNED:", res.status, res.reason)
    except BaseException as exc:
        print("PROBE-A RAISED:", type(exc).__name__, repr(exc)[:300])
    print("PROBE-A CALLS:", calls)
    rec = harness.store.load(run_id)
    print(
        "PROBE-A STATUS AFTER:", rec.status, "pid:", rec.pid,
        "steps:", {k: v.status.value for k, v in rec.steps.items()},
    )


async def test_probe_b_join_always_composites_after_cancel(harness: Harness) -> None:
    harness.workflow(
        "t",
        """rayspec: 1
name: t
steps:
  - id: work
    shell: sleep 2
  - id: cleanup_loop
    needs: [work]
    join: always
    loop:
      max_iterations: 1
      steps:
        - id: c
          shell: echo loop-cleaned
  - id: cleanup_each
    needs: [work]
    join: always
    each: "[1]"
    steps:
      - id: e
        shell: echo each-cleaned
  - id: cleanup_leaf
    needs: [work]
    join: always
    shell: echo leaf-cleaned
""",
    )
    run_id = new_run_id()
    runner = harness.runner("t", options=RunOptions(interactive=False), run_id=run_id)

    async def flag() -> None:
        await anyio.sleep(0.7)
        write_cancel_flag(harness.store.run_dir(run_id), reason="probe cancel")

    async with anyio.create_task_group() as tg:
        tg.start_soon(flag)
        res = await runner.run()
    print("PROBE-B RESULT:", res.status, res.reason)
    rec = harness.store.load(run_id)
    for k, v in rec.steps.items():
        print("PROBE-B STEP", k, v.status.value, "skip=", v.skip_reason, "err=", v.error)
