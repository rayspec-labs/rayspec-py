# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R4/R5: a resume's liveness guard uses the shared ``assess`` rule (a reused pid no
longer blocks a resume), and a resume clears a stale ``cancel.json`` so a cancelled run can be
resumed at all."""

from __future__ import annotations

import pytest

from rayspec.engine import liveness
from rayspec.engine.cancel import read_cancel_flag, write_cancel_flag
from rayspec.engine.context import RunOptions
from rayspec.engine.errors import ResumeError
from rayspec.schema import RunStatus

from .conftest import Harness

pytestmark = pytest.mark.anyio


def wf(steps: str) -> str:
    return f"rayspec: 1\nname: t\nsteps:\n{steps}"


async def _paused_run(harness: Harness) -> str:
    """A run paused at a gate — a record on disk to doctor into a 'running' shape and resume."""
    harness.workflow(
        "t",
        wf("  - {id: a, shell: echo hi}\n  - id: g\n    needs: [a]\n    approve: ship it\n"),
    )
    result = await harness.run("t", options=RunOptions(interactive=False))
    assert result.status is RunStatus.PAUSED
    return result.run_id


def _make_running(harness: Harness, run_id: str, **fields: object) -> None:
    run = harness.store.load(run_id)
    run.status = RunStatus.RUNNING
    for k, v in fields.items():
        setattr(run, k, v)
    harness.store.save(run)


async def test_resume_refuses_a_live_process(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await _paused_run(harness)
    _make_running(harness, run_id, pid=4242, pid_started_at="T0")
    monkeypatch.setattr(liveness, "assess", lambda run, **_: liveness.Liveness.ALIVE)
    with pytest.raises(ResumeError, match="still running"):
        await harness.run("t", resume=run_id, options=RunOptions(interactive=False))


async def test_resume_refuses_a_stale_heartbeat_with_a_wedged_hint(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await _paused_run(harness)
    _make_running(harness, run_id, pid=4242, pid_started_at="T0")
    monkeypatch.setattr(liveness, "assess", lambda run, **_: liveness.Liveness.STALE_HEARTBEAT)
    with pytest.raises(ResumeError) as exc:
        await harness.run("t", resume=run_id, options=RunOptions(interactive=False))
    assert "wedged" in str(exc.value) or "--now" in str(getattr(exc.value, "hint", ""))


@pytest.mark.parametrize("verdict", [liveness.Liveness.DEAD_PID, liveness.Liveness.PID_REUSED])
async def test_resume_proceeds_on_a_dead_or_reused_pid(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, verdict: liveness.Liveness
) -> None:
    run_id = await _paused_run(harness)
    _make_running(harness, run_id, pid=4242, pid_started_at="T0")
    monkeypatch.setattr(liveness, "assess", lambda run, **_: verdict)
    result = await harness.run("t", resume=run_id, options=RunOptions(interactive=False, yes=True))
    assert result.status is RunStatus.SUCCEEDED


async def test_resume_clears_a_stale_cancel_flag(harness: Harness) -> None:
    run_id = await _paused_run(harness)
    write_cancel_flag(harness.store.run_dir(run_id), reason="an earlier cancel")
    result = await harness.run("t", resume=run_id, options=RunOptions(interactive=False, yes=True))
    assert result.status is RunStatus.SUCCEEDED
    assert read_cancel_flag(harness.store.run_dir(run_id)) is None
