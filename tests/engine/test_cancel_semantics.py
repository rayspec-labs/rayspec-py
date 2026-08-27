# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R5: cooperative cancel — a cancel drains the run (exit 4) and lets ``join: always``
cleanups run, but it never outranks a genuine failure, and its own teardown (a drained step, a
loop cut between iterations) is collateral, not a failure of the run."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from rayspec.engine.cancel import read_cancel_flag, write_cancel_flag
from rayspec.engine.context import CANCEL_SKIP_REASON, RunOptions
from rayspec.engine.runner import Runner
from rayspec.schema import RunStatus, StepStatus

from .conftest import FakeLeaf, Harness

pytestmark = pytest.mark.anyio


def wf(steps: str, *, defaults: str = "") -> str:
    block = f"defaults:\n{defaults}" if defaults else ""
    return f"rayspec: 1\nname: t\n{block}steps:\n{steps}"


async def run_cancelling(harness: Harness, name: str, *, at: str, leaf: FakeLeaf) -> Any:
    """Run ``name``; once the step at path ``at`` is in flight, write cancel.json into its run
    dir and release it. Returns the RunResult."""
    result_box: list[Any] = []

    async def drive() -> None:
        runner = Runner(
            harness.load(name),
            inputs={},
            store=harness.store,
            sinks=harness.sink,
            project_root=harness.root,
            project_slug="local/test",
            engine=harness.engine,
            options=RunOptions(interactive=False),
            executors={"shell": leaf, "python": leaf},
            handle_signals=True,
        )
        result_box.append(await runner.run())

    async with anyio.create_task_group() as tg:
        tg.start_soon(drive)
        await leaf.wait_started(at)
        # the run dir exists once a step has started; find it by the single run under the store
        run_id = next(iter(harness.store.list_run_ids()))
        write_cancel_flag(harness.store.run_dir(run_id), reason="operator asked")
        leaf.releases["hold"].set()
    return result_box[0]


async def test_join_always_leaf_runs_after_cancel(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  - {id: work, shell: 'block:hold'}\n"
            "  - id: cleanup\n    needs: [work]\n    join: always\n    shell: ok\n"
        ),
    )
    leaf = FakeLeaf()
    result = await run_cancelling(harness, "t", at="work", leaf=leaf)
    assert result.status is RunStatus.CANCELLED
    assert result.exit_code == 4
    assert result.steps["cleanup"].status is StepStatus.SUCCEEDED


async def test_failed_beats_cancelled(harness: Harness) -> None:
    """A step that failed before the cancel makes the run FAILED (exit 1), not cancelled."""
    harness.workflow(
        "t",
        wf(
            "  - {id: bad, shell: fail}\n"
            "  - {id: work, needs: [bad], join: always, shell: 'block:hold'}\n",
            defaults="  on_step_failure: continue\n",
        ),
    )
    leaf = FakeLeaf()
    result = await run_cancelling(harness, "t", at="work", leaf=leaf)
    assert result.status is RunStatus.FAILED
    assert result.exit_code == 1
    assert "bad" in (result.reason or "")


async def test_cancelled_loop_is_interrupted_collateral_not_failed(harness: Harness) -> None:
    """A loop cut between iterations records interrupted+cancelled (collateral), keeps its
    LoopInfo, and does not make the run FAILED."""
    harness.workflow(
        "t",
        wf(
            "  - id: build\n    loop:\n      max_iterations: 5\n"
            "      steps:\n        - {id: step, shell: 'block:hold'}\n"
        ),
    )
    leaf = FakeLeaf()
    result = await run_cancelling(harness, "t", at="build[1]/step", leaf=leaf)
    assert result.status is RunStatus.CANCELLED
    build = result.steps["build"]
    assert build.status is StepStatus.INTERRUPTED
    assert build.skip_reason == CANCEL_SKIP_REASON
    assert build.loop is not None and build.loop.iterations >= 1


async def test_flag_cleared_after_finalize(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  - {id: work, shell: 'block:hold'}\n  - {id: c, needs: [work], join: always, shell: ok}\n"
        ),
    )
    leaf = FakeLeaf()
    result = await run_cancelling(harness, "t", at="work", leaf=leaf)
    assert result.status is RunStatus.CANCELLED
    assert read_cancel_flag(harness.store.run_dir(result.run_id)) is None
