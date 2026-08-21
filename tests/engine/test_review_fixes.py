"""Engine-side review fixes: stale resume cache after an interrupted ``--force``
resume, ``run.pause`` cleared on every decision path, cross-workflow / other-host resume refusals,
``each:`` over arbitrary iterables, a broken console sink never failing the run, the workdir
path lock and ``timeout:`` on approve/stop steps."""

from __future__ import annotations

import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.errors import EngineError, ResumeError
from rayspec.engine.runtime import Runtime
from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.schema import RunStatus, StepStatus
from rayspec.store.model import Decision

from .conftest import Harness

pytestmark = pytest.mark.anyio


def wf(steps: str, **top: str) -> str:
    extra = "".join(f"{k}:\n{v}\n" for k, v in top.items())
    return f"rayspec: 1\nname: t\n{extra}steps:\n{steps}"


# --------------------------------------------------------------------------------------------------
# resume cache
# --------------------------------------------------------------------------------------------------


async def test_plain_resume_after_interrupted_forced_resume_reruns_stale_downstream(
    harness: Harness,
) -> None:
    """A forced resume stamps the new hash first; if it dies before ``b`` is re-decided, the
    next plain resume must still notice that ``b``'s input (``a``'s output) changed."""
    harness.workflow(
        "t",
        wf(
            "  - {id: a, shell: echo v1}\n"
            "  - {id: b, needs: [a], shell: 'echo b-{{ steps.a.output }}'}\n"
            "  - {id: c, needs: [b], shell: exit 1}"
        ),
    )
    first = await harness.run("t", run_id="20260820-000000-stal")
    assert first.status is RunStatus.FAILED
    harness.workflow(
        "t",
        wf(
            "  - {id: a, shell: echo v2}\n"
            "  - {id: b, needs: [a], shell: 'echo b-{{ steps.a.output }}'}\n"
            "  - {id: c, needs: [b], shell: exit 0}"
        ),
    )
    new_hash = harness.load("t").hash
    # simulate the interrupted forced resume: hash stamped, a interrupted, b still cached from v1
    run = harness.record("20260820-000000-stal")
    run.workflow_hash = new_hash
    run.status = RunStatus.INTERRUPTED
    run.steps["a"].status = StepStatus.INTERRUPTED
    harness.store.save(run)
    harness.sink.clear()
    result = await harness.run("t", resume="20260820-000000-stal")
    assert result.status is RunStatus.SUCCEEDED
    assert "b" not in result.reused
    assert (
        harness.store.read_output("20260820-000000-stal", result.steps["b"].output_ref or "")
        == "b-v2"
    )


async def test_resume_refuses_a_run_of_another_workflow(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: a, shell: exit 1}"))
    harness.workflow("u", "rayspec: 1\nname: u\nsteps:\n  - {id: a, shell: echo hi}")
    first = await harness.run("t", run_id="20260820-000000-othr")
    assert first.status is RunStatus.FAILED
    with pytest.raises(ResumeError, match="belongs to workflow 't', not 'u'"):
        await harness.run("u", resume="20260820-000000-othr")
    # --force never crosses workflows
    with pytest.raises(ResumeError, match="belongs to workflow"):
        await harness.run("u", resume="20260820-000000-othr", options=RunOptions(force=True))
    assert harness.record("20260820-000000-othr").workflow_name == "t"


async def test_resume_refuses_a_run_recorded_running_on_another_host(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: a, shell: echo a}"))
    first = await harness.run("t", run_id="20260820-000000-host")
    run = harness.record(first.run_id)
    run.status = RunStatus.RUNNING
    run.host = "some-other-machine.example"
    run.pid = 4242
    harness.store.save(run)
    with pytest.raises(ResumeError, match=r"running on host some-other-machine\.example"):
        await harness.run("t", resume=first.run_id)
    forced = await harness.run("t", resume=first.run_id, options=RunOptions(force=True))
    assert forced.status is RunStatus.SUCCEEDED


# --------------------------------------------------------------------------------------------------
# approval gates
# --------------------------------------------------------------------------------------------------


async def test_gate_answered_with_yes_on_resume_clears_run_pause(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  - {id: a, shell: echo built}\n"
            "  - {id: gate, needs: [a], approve: 'ship?'}\n"
            "  - {id: ship, needs: [gate], shell: echo shipped}"
        ),
    )
    paused = await harness.run("t", options=RunOptions(interactive=False))
    assert paused.status is RunStatus.PAUSED and paused.pause is not None
    harness.sink.clear()
    resumed = await harness.run(
        "t", resume=paused.run_id, options=RunOptions(interactive=False, yes=True)
    )
    assert resumed.status is RunStatus.SUCCEEDED
    assert resumed.pause is None
    assert harness.record(paused.run_id).pause is None


async def test_gate_answered_by_stored_decision_still_clears_pause(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: gate, approve: 'ok?'}"))
    paused = await harness.run("t", options=RunOptions(interactive=False))
    run = harness.record(paused.run_id)
    assert run.pause is not None
    run.pause.decision = Decision(approved=True, comment="", by="cli")
    harness.store.save(run)
    resumed = await harness.run("t", resume=paused.run_id, options=RunOptions(interactive=False))
    assert resumed.status is RunStatus.SUCCEEDED and resumed.pause is None


# --------------------------------------------------------------------------------------------------
# each: over any iterable
# --------------------------------------------------------------------------------------------------


async def test_each_accepts_dict_values_and_range(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  - id: vals\n"
            "    each: inputs.m.values()\n"
            "    steps: [{id: w, shell: 'echo {{ item }}'}]\n"
            "  - id: rng\n"
            "    each: range(3)\n"
            "    steps: [{id: x, shell: 'echo {{ item }}'}]\n",
            inputs="  m: {type: object, default: {a: 1, b: 2}}",
        ),
    )
    result = await harness.run("t", {"m": {"a": 1, "b": 2}})
    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert result.steps["vals"].each is not None and result.steps["vals"].each.total == 2
    assert result.steps["rng"].each is not None and result.steps["rng"].each.total == 3


# --------------------------------------------------------------------------------------------------
# a sink that dies (broken pipe / SystemExit from Rich) never changes the run status
# --------------------------------------------------------------------------------------------------


class _BrokenSink:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def emit(self, event: RunEvent) -> None:
        self.calls += 1
        raise self.exc

    async def emit_stream(self, step_path: str, record: StreamRecord) -> None:
        raise self.exc

    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize("exc", [BrokenPipeError(32, "Broken pipe"), SystemExit(1)])
async def test_broken_console_sink_does_not_fail_the_run(
    harness: Harness, exc: BaseException
) -> None:
    harness.workflow("t", wf("  - {id: a, shell: echo a}\n  - {id: b, needs: [a], shell: echo b}"))
    sink = _BrokenSink(exc)
    runner = harness.runner("t", run_id="20260820-000000-pipe")
    runner.sinks = sink  # type: ignore[assignment]
    result = await runner.run()
    assert result.status is RunStatus.SUCCEEDED, result.reason
    assert sink.calls == 1  # the sink is dropped after its first failure
    assert harness.record("20260820-000000-pipe").status is RunStatus.SUCCEEDED
    # the store still has every event
    events = harness.store.read_events("20260820-000000-pipe")
    assert any(e.type is EventType.RUN_FINISHED for e in events)


# --------------------------------------------------------------------------------------------------
# workdir path lock
# --------------------------------------------------------------------------------------------------


async def test_runner_takes_and_releases_the_workdir_lock(harness: Harness) -> None:
    pytest.importorskip("fcntl")
    from rayspec.workspace import PathLock

    harness.workflow("t", wf("  - {id: a, shell: echo a}"))
    lock = PathLock(harness.home, "local/test", harness.root, run_id="other")
    lock.acquire()
    try:
        runner = harness.runner("t", run_id="20260820-000000-lck1")
        runner.home = harness.home
        with pytest.raises(EngineError, match="already locked by run other"):
            await runner.run()
    finally:
        lock.release()
    runner = harness.runner("t", run_id="20260820-000000-lck2")
    runner.home = harness.home
    result = await runner.run()
    assert result.status is RunStatus.SUCCEEDED
    # released after the run: somebody else can take it
    assert lock.acquire().held
    lock.release()


async def test_lock_released_on_pause_and_retaken_on_resume(harness: Harness) -> None:
    pytest.importorskip("fcntl")
    from rayspec.workspace import PathLock

    harness.workflow("t", wf("  - {id: gate, approve: 'ok?'}"))
    runner = harness.runner("t", options=RunOptions(interactive=False))
    runner.home = harness.home
    paused = await runner.run()
    assert paused.status is RunStatus.PAUSED
    probe = PathLock(harness.home, "local/test", harness.root, run_id="probe")
    assert probe.acquire().held
    probe.release()
    resumed = harness.runner("t", resume=paused.run_id, options=RunOptions(yes=True))
    resumed.home = harness.home
    assert (await resumed.run()).status is RunStatus.SUCCEEDED


# --------------------------------------------------------------------------------------------------
# quiesce: wait_quiesced only returns when no leaf is active
# --------------------------------------------------------------------------------------------------


async def test_wait_quiesced_loops_until_no_leaf_is_active() -> None:
    import anyio

    rt = Runtime(1)
    first_done = anyio.Event()
    second_started = anyio.Event()

    async def leaf(started: anyio.Event | None, wait_for: anyio.Event | None) -> None:
        async with rt.leaf_permit():
            if started is not None:
                started.set()
            if wait_for is not None:
                await wait_for.wait()

    quiesced = False

    async def gate() -> None:
        nonlocal quiesced
        rt.close_gate()
        await rt.wait_quiesced()
        quiesced = True
        assert rt.active_leaves == 0
        rt.open_gate()

    async with anyio.create_task_group() as tg:
        tg.start_soon(leaf, None, first_done)  # holds the single slot
        await anyio.sleep(0.01)
        tg.start_soon(leaf, second_started, None)  # passed wait_launch, blocked on the limiter
        await anyio.sleep(0.01)
        tg.start_soon(gate)
        await anyio.sleep(0.01)
        first_done.set()  # the second leaf grabs the slot right as the first goes idle
        await anyio.sleep(0.05)
    assert quiesced and second_started.is_set()


# --------------------------------------------------------------------------------------------------
# timeout on approve/stop steps is rejected at validation time
# --------------------------------------------------------------------------------------------------


def test_validate_rejects_timeout_on_approve_and_stop(harness: Harness) -> None:
    from rayspec.loader import validate_workflow

    harness.workflow(
        "t",
        wf(
            "  - {id: gate, approve: 'ok?', timeout: 1s}\n"
            "  - {id: halt, needs: [gate], stop: {status: cancelled}, timeout: 2s}"
        ),
    )
    report = validate_workflow(harness.load("t"))
    assert any("timeout" in e and "gate" in e for e in report.errors), report.errors
    assert any("timeout" in e and "halt" in e for e in report.errors), report.errors


# --------------------------------------------------------------------------------------------------
# RAYSPEC_CONTEXT never persists the process environment
# --------------------------------------------------------------------------------------------------


async def test_context_file_has_no_env_root(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: a, shell: 'cat \"$RAYSPEC_CONTEXT\"'}"))
    result = await harness.run("t", env={"ANTHROPIC_API_KEY": "sk-secret-123", "PATH": "/bin"})
    assert result.status is RunStatus.SUCCEEDED, result.reason
    text = (harness.store.step_dir(result.run_id, "a") / "context.json").read_text()
    assert "sk-secret-123" not in text
    assert '"env"' not in text
