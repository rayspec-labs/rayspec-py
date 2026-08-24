"""engine.runner: lifecycle, status/exit codes, outputs, resume reuse (interrupt, loop mid-way,
each partial, hash refuse/--force, always_run), pause → approve → resume, SIGINT."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import anyio
import pytest
from anyio import to_thread

from events._validating import ValidatingSink
from rayspec.engine.approval import ApprovalAnswer, ApprovalRequest
from rayspec.engine.context import RunOptions
from rayspec.engine.errors import ResumeError
from rayspec.engine.runner import Runner, RunResult, Workspace, fallback_project_slug
from rayspec.events.model import EventType
from rayspec.providers.stub import StubProvider
from rayspec.schema import RunStatus, StepStatus
from rayspec.store.model import Decision

from .conftest import Harness

pytestmark = pytest.mark.anyio


def wf(steps: str, **top: str) -> str:
    extra = "".join(f"{k}:\n{v}\n" for k, v in top.items())
    return f"rayspec: 1\nname: t\n{extra}steps:\n{steps}"


async def wait_until_running(marker: Path) -> None:
    """Block until the step whose body touches ``marker`` has begun executing.

    The moment a run becomes interruptible is a state, not a duration: reaching it takes two
    real subprocess spawns and several fsynced ``run.json`` writes, timed between 0.3s and
    0.6s on an idle machine and further under load. ``fail_after`` is the hang detector — a run
    that never gets there says so, instead of a signal being fired into a run that has not
    started and a confusing ``KeyError`` several assertions later.

    The step's own first command is the observable, and the reason is worth stating because two
    nearer ones look right and are not. ``run.json`` carries a record for the step before the
    leaf has taken a concurrency permit, and ``step.started`` is emitted earlier still — while
    ``attempts`` is incremented only once the permit is held (`engine/scheduler.py`). A run
    interrupted between the record and the increment persists a record carrying no attempt, so
    the resume runs the step for what it correctly believes is the first time, and an assertion
    about ``attempts`` after the resume then fails for a reason that has nothing to do with what
    it is about. Waiting for the record alone did exactly that: two runs in fifteen on a loaded
    machine, and one CI cell out of four. Touching a file from the body is the earliest thing
    that is unambiguously past the increment, because the body does not run until it is.
    """
    with anyio.fail_after(15):
        while not marker.exists():
            await anyio.sleep(0.01)


async def test_run_succeeds_with_outputs_and_events(harness: Harness) -> None:
    harness.sink = ValidatingSink(harness.sink)  # pin the published event/stream shapes
    harness.workflow(
        "t",
        wf(
            """
  - {id: a, shell: "echo hello {{ inputs.who }}"}
  - {id: b, needs: [a], prompt: "say {{ steps.a.output }}"}
""",
            inputs="  who: {type: string, default: world}",
            outputs='  greeting: "{{ steps.a.output }}"\n  reply: "{{ steps.b.output }}"\n  n: "{{ 1 + 1 }}"',
        ),
    )
    stub = StubProvider(script={"steps": {"b": {"text": "hi!"}}})
    result = await harness.run("t", {"who": "world"}, providers={"claude": stub})
    assert isinstance(result, RunResult)
    assert result.status is RunStatus.SUCCEEDED and result.exit_code == 0
    assert result.outputs == {"greeting": "hello world", "reply": "hi!", "n": 2}
    assert result.usage.total > 0
    run = harness.record(result.run_id)
    assert run.status is RunStatus.SUCCEEDED and run.outputs == result.outputs
    assert run.inputs == {"who": "world"} and run.pid is None and run.ended_at is not None
    assert run.workspace.isolation == "none" and run.workspace.workdir == str(harness.root)
    assert run.project_slug == "local/test" and run.workflow_hash
    types = [e.type for e in harness.events()]
    assert types[0] is EventType.RUN_STARTED and types[-1] is EventType.RUN_FINISHED
    assert harness.events(EventType.RUN_FINISHED)[0].data["status"] == "succeeded"
    # persisted events.jsonl mirrors the sink
    stored = list(harness.store.read_events(result.run_id))
    assert [e.type for e in stored] == types
    assert stub.closed is True  # providers are closed per run


async def test_on_step_failure_continue_still_fails_the_run(harness: Harness) -> None:
    """Independent branches finish, but a failed step still fails the run — loudly."""
    harness.workflow(
        "t",
        wf(
            "  - {id: bad, shell: 'exit 7'}\n"
            "  - {id: after, needs: [bad], shell: echo x}\n"
            "  - {id: indep, shell: echo ok}\n",
            defaults="  on_step_failure: continue",
        ),
    )
    result = await harness.run("t")
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    assert result.reason and "step 'bad' failed" in result.reason
    assert result.steps["indep"].status is StepStatus.SUCCEEDED  # the branch kept going
    assert result.steps["after"].status is StepStatus.SKIPPED  # its own dependents still skip
    assert result.steps["after"].skip_reason == "upstream_failed"


async def test_run_failed_step_gives_exit_1_and_reason(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("  - {id: a, shell: 'echo bad >&2; exit 7'}\n  - {id: b, needs: [a], shell: echo x}"),
    )
    result = await harness.run("t")
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    assert result.reason and "step 'a' failed" in result.reason and "exit code 7" in result.reason
    assert result.steps["b"].status is StepStatus.SKIPPED
    assert result.outputs is None


async def test_stop_cannot_launder_a_failure_under_continue(harness: Harness) -> None:
    """Regression: a `stop:` step must never turn a failed run green.

    Under `drain` a `stop:` on an independent branch was unreachable after a failure, so
    `_finalize` could safely let `ctx.stopped` decide the run status. `continue` re-opens the
    ready-set, so the stop step now runs — and must NOT outrank an untolerated failure. Otherwise
    a fan-out-checks-then-report workflow reports success, exit 0, and publishes `outputs:` while
    `run.json` holds a failed step: a green CI on a red run.
    """
    harness.workflow(
        "t",
        wf(
            """
  - {id: boom, shell: 'exit 7'}
  - {id: wait, shell: 'echo waited'}
  - {id: finish, needs: [wait], stop: {status: succeeded, reason: "report published"}}
""",
            defaults="  on_step_failure: continue",
            outputs='  boom_ok: "{{ steps.boom.ok }}"',
        ),
    )
    result = await harness.run("t")
    assert result.status is RunStatus.FAILED, "a stop: must not launder an untolerated failure"
    assert result.exit_code == 1
    assert result.reason and "boom" in result.reason
    assert result.outputs is None, "outputs must not be published for a failed run"
    assert harness.record(result.run_id).status is RunStatus.FAILED  # persisted, not just in memory


async def test_stop_still_decides_the_status_when_nothing_failed(harness: Harness) -> None:
    """The guard above must not break the normal case: no failure → `stop:` still wins."""
    harness.workflow(
        "t",
        wf(
            """
  - {id: a, shell: echo go}
  - {id: done, needs: [a], stop: {status: succeeded, reason: "early"}}
""",
            defaults="  on_step_failure: continue",
            outputs='  a: "{{ steps.a.output }}"',
        ),
    )
    result = await harness.run("t")
    assert result.status is RunStatus.SUCCEEDED and result.exit_code == 0
    assert result.reason == "early"
    assert result.outputs == {"a": "go"}


async def test_stop_cancelled_and_succeeded(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            """
  - {id: a, shell: echo go}
  - {id: halt, needs: [a], when: "inputs.mode == 'cancel'", stop: {status: cancelled, reason: "nope"}}
  - {id: done, needs: [a], when: "inputs.mode == 'ok'", stop: {status: succeeded, reason: "early"}}
  - {id: never, needs: [halt, done], join: any, shell: echo never}
""",
            inputs="  mode: {type: string, default: cancel}",
            outputs='  a: "{{ steps.a.output }}"',
        ),
    )
    result = await harness.run("t", {"mode": "cancel"})
    assert result.status is RunStatus.CANCELLED and result.exit_code == 4
    assert result.reason == "nope"
    assert harness.record(result.run_id).status is RunStatus.CANCELLED
    result2 = await harness.run("t", {"mode": "ok"})
    assert result2.status is RunStatus.SUCCEEDED and result2.exit_code == 0
    assert result2.outputs == {"a": "go"} and result2.reason == "early"
    assert "never" not in result2.steps or result2.steps["never"].status is StepStatus.SKIPPED


async def test_outputs_render_error_fails_run(harness: Harness) -> None:
    harness.workflow(
        "t", wf("  - {id: a, shell: echo x}", outputs='  v: "{{ steps.a.output.field }}"')
    )
    result = await harness.run("t")
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    assert result.reason and result.reason.startswith("outputs:")


async def test_pause_then_approve_resume(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: echo built}
  - {id: gate, needs: [a], approve: "ship?"}
  - {id: ship, needs: [gate], shell: "echo shipped {{ steps.gate.output }}"}
"""),
    )
    result = await harness.run("t", options=RunOptions(interactive=False))
    assert result.status is RunStatus.PAUSED and result.exit_code == 3
    assert result.pause is not None and result.pause.token == "gate#1"
    run = harness.record(result.run_id)
    assert run.status is RunStatus.PAUSED and run.pid is not None
    # rayspec approve writes the decision, then resumes in-process
    run.pause.decision = Decision(approved=True, comment="go", by="cli")  # type: ignore[union-attr]
    harness.store.save(run)
    harness.sink.clear()
    resumed = await harness.run("t", resume=result.run_id, options=RunOptions(interactive=False))
    assert resumed.status is RunStatus.SUCCEEDED and resumed.exit_code == 0
    assert resumed.run_id == result.run_id
    assert resumed.reused == ["a"]
    assert (
        resumed.steps["gate"].status is StepStatus.SUCCEEDED and resumed.steps["gate"].attempts == 1
    )
    assert resumed.steps["ship"].status is StepStatus.SUCCEEDED
    assert (
        harness.store.read_output(result.run_id, resumed.steps["ship"].output_ref or "")
        == "shipped go"
    )
    rec = harness.record(result.run_id)
    assert rec.resume_count == 1 and rec.pause is None
    assert harness.events(EventType.RUN_RESUMED)


async def test_resume_after_interrupt_reuses_finished_steps(
    harness: Harness, tmp_path: Path
) -> None:
    flag = tmp_path / "flag"
    running = tmp_path / "b-running"
    harness.workflow(
        "t",
        wf(f"""
  - {{id: a, shell: echo a}}
  - {{id: b, needs: [a], shell: "touch {running}; test -f {flag} || sleep 30; echo b"}}
  - {{id: c, needs: [b], shell: echo c}}
"""),
    )
    runner = harness.runner("t", run_id="20260820-000000-intr")
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.run)
        await wait_until_running(running)
        tg.cancel_scope.cancel()  # interrupt with ``b``'s body provably executing
    run = harness.record("20260820-000000-intr")
    assert run.status is RunStatus.INTERRUPTED
    assert run.steps["a"].status is StepStatus.SUCCEEDED
    assert run.steps["b"].status is StepStatus.INTERRUPTED
    assert "c" not in run.steps
    flag.write_text("1")
    harness.sink.clear()
    result = await harness.run("t", resume="20260820-000000-intr")
    assert result.status is RunStatus.SUCCEEDED
    assert result.reused == ["a"]
    assert result.steps["b"].attempts == 2 and result.steps["c"].attempts == 1
    finished = harness.finished("a")
    assert finished.data.get("reused") is True
    assert [e.step_path for e in harness.events(EventType.STEP_STARTED)] == ["b", "c"]


async def test_resume_loop_mid_way(harness: Harness, tmp_path: Path) -> None:
    flag = tmp_path / "flag"
    harness.workflow(
        "t",
        wf(f"""
  - id: build
    loop:
      max_iterations: 3
      until: steps.check.output == 'pass'
      steps:
        - {{id: impl, shell: "echo impl{{{{ iteration.n }}}}"}}
        - id: check
          needs: [impl]
          shell: |
            if [ {{{{ iteration.n }}}} -eq 2 ] && [ ! -f {flag} ]; then echo crash >&2; exit 1; fi
            if [ {{{{ iteration.n }}}} -ge 2 ]; then echo pass; else echo fail; fi
"""),
    )
    first = await harness.run("t", run_id="20260820-000000-loop")
    assert first.status is RunStatus.FAILED
    assert first.steps["build[2]/check"].status is StepStatus.FAILED
    assert first.steps["build"].loop is not None and first.steps["build"].loop.iterations == 2
    flag.write_text("1")
    harness.sink.clear()
    second = await harness.run("t", resume="20260820-000000-loop")
    assert second.status is RunStatus.SUCCEEDED
    assert sorted(second.reused) == ["build[1]/check", "build[1]/impl", "build[2]/impl"]
    assert second.steps["build[2]/check"].attempts == 2
    assert second.steps["build"].loop is not None and second.steps["build"].loop.converged is True
    assert second.steps["build"].loop.iterations == 2


async def test_resume_each_partial_and_item_sha(harness: Harness, tmp_path: Path) -> None:
    flag = tmp_path / "flag"
    harness.workflow(
        "t",
        wf(
            f"""
  - id: fan
    each: inputs.items
    steps:
      - id: w
        shell: |
          if [ "{{{{ item }}}}" = "b" ] && [ ! -f {flag} ]; then exit 1; fi
          echo did-{{{{ item }}}}
""",
            inputs="  items: {type: array, default: [a, b, c]}",
        ),
    )
    first = await harness.run("t", {"items": ["a", "b", "c"]}, run_id="20260820-000000-each")
    assert first.status is RunStatus.FAILED
    assert first.steps["fan[1]/w"].status is StepStatus.FAILED
    flag.write_text("1")
    harness.sink.clear()
    second = await harness.run("t", resume="20260820-000000-each")
    assert second.status is RunStatus.SUCCEEDED
    assert sorted(second.reused) == ["fan[0]/w", "fan[2]/w"]
    assert second.steps["fan[1]/w"].attempts == 2
    # a changed item sha (simulated) re-runs the item with a warning
    run = harness.record("20260820-000000-each")
    run.steps["fan[2]/w"].item_sha256 = "0" * 64
    harness.store.save(run)
    harness.sink.clear()
    third = await harness.run("t", resume="20260820-000000-each")
    assert sorted(third.reused) == ["fan[0]/w", "fan[1]/w"]
    warnings = harness.events(EventType.WARNING)
    assert warnings and "item changed" in warnings[0].data["message"]


async def test_resume_hash_mismatch_refuses_unless_force(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: a, shell: echo a}\n  - {id: b, needs: [a], shell: echo b}"))
    first = await harness.run("t", run_id="20260820-000000-hash")
    assert first.status is RunStatus.SUCCEEDED
    # change the workflow: b's script differs, a is identical
    harness.workflow("t", wf("  - {id: a, shell: echo a}\n  - {id: b, needs: [a], shell: echo b2}"))
    with pytest.raises(ResumeError, match="changed since run"):
        await harness.run("t", resume="20260820-000000-hash")
    harness.sink.clear()
    forced = await harness.run("t", resume="20260820-000000-hash", options=RunOptions(force=True))
    assert forced.status is RunStatus.SUCCEEDED
    assert forced.reused == ["a"]  # fingerprint of a unchanged; b re-ran
    assert forced.steps["b"].attempts == 2
    assert (
        harness.store.read_output("20260820-000000-hash", forced.steps["b"].output_ref or "")
        == "b2"
    )
    warnings = [e.data["message"] for e in harness.events(EventType.WARNING)]
    assert any("fingerprint mismatch" in w for w in warnings)


async def test_resume_always_run_and_finished_runs_refused(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf(
            "  - {id: a, shell: echo a}\n  - {id: b, shell: echo b, always_run: true}\n  - {id: c, needs: [a, b], shell: 'exit 1'}"
        ),
    )
    first = await harness.run("t", run_id="20260820-000000-alw")
    assert first.status is RunStatus.FAILED
    harness.workflow(
        "t",
        wf(
            "  - {id: a, shell: echo a}\n  - {id: b, shell: echo b, always_run: true}\n  - {id: c, needs: [a, b], shell: 'exit 1'}"
        ),
    )
    second = await harness.run("t", resume="20260820-000000-alw")
    assert second.reused == ["a"]
    assert second.steps["b"].attempts == 2 and second.steps["c"].attempts == 2
    # inputs are fixed per run: the record's inputs win over whatever the caller passes
    harness.workflow(
        "u", wf("  - {id: a, shell: echo ok}", inputs="  x: {type: string, default: d}")
    )
    ok = await harness.run("u", {"x": "first"}, run_id="20260820-000000-inp")
    assert ok.status is RunStatus.SUCCEEDED
    again = await harness.run("u", {"x": "second"}, resume="20260820-000000-inp")
    assert again.reused == ["a"] and harness.record("20260820-000000-inp").inputs == {"x": "first"}


async def test_sigint_interrupts_run_exit_130(harness: Harness, tmp_path: Path) -> None:
    running = tmp_path / "b-running"
    harness.workflow(
        "t",
        wf(
            f'  - {{id: a, shell: echo a}}\n  - {{id: b, needs: [a], shell: "touch {running}; sleep 30"}}'
        ),
    )
    runner = harness.runner("t", run_id="20260820-000000-sig")

    async def fire() -> None:
        # wait until b's body is actually executing, then Ctrl-C ourselves
        await wait_until_running(running)
        os.kill(os.getpid(), signal.SIGINT)

    result: RunResult | None = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(fire)
        result = await runner.run()
    assert result is not None
    assert result.status is RunStatus.INTERRUPTED and result.exit_code == 130
    assert result.interrupted is True
    run = harness.record("20260820-000000-sig")
    assert run.status is RunStatus.INTERRUPTED and run.steps["b"].status is StepStatus.INTERRUPTED
    assert harness.events(EventType.RUN_FINISHED)[0].data["status"] == "interrupted"


async def test_workspace_info_recorded_and_event(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: a, shell: 'echo $RAYSPEC_WORKDIR'}"))
    ws = Workspace(
        isolation="worktree",
        workdir=harness.root,
        branch="rayspec/t-abcd",
        base_branch="main",
        base_sha="deadbeef",
    )
    result = await harness.run("t", workspace=ws)
    assert result.status is RunStatus.SUCCEEDED
    run = harness.record(result.run_id)
    assert run.workspace.branch == "rayspec/t-abcd" and run.workspace.base_sha == "deadbeef"
    created = harness.events(EventType.WORKSPACE_CREATED)
    assert created and created[0].data["branch"] == "rayspec/t-abcd"
    assert harness.store.read_output(
        result.run_id, result.steps["a"].output_ref or ""
    ).strip() == str(harness.root)


def test_fallback_project_slug(tmp_path: Path) -> None:
    slug = fallback_project_slug(tmp_path / "my-repo")
    assert slug.startswith("local/my-repo-") and len(slug.split("-")[-1]) == 8
    assert slug == fallback_project_slug(tmp_path / "my-repo")


async def test_run_sync_entry_point(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: a, shell: echo sync}"))
    runner = harness.runner("t")
    result = await to_thread.run_sync(Runner.run_sync, runner)
    assert result.status is RunStatus.SUCCEEDED


async def test_engine_bug_finalizes_run_as_failed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rayspec.engine.runner as runner_mod

    async def broken(*args, **kwargs):
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(runner_mod, "run_graph", broken)
    harness.workflow("t", wf("  - {id: a, shell: echo a}"))
    result = await harness.run("t", run_id="20260820-000000-bug")
    assert result.status is RunStatus.FAILED and result.exit_code == 1
    assert (
        result.reason and "engine error" in result.reason and "scheduler exploded" in result.reason
    )
    run = harness.record("20260820-000000-bug")
    assert run.status is RunStatus.FAILED and run.reason == result.reason
    assert harness.events(EventType.RUN_FINISHED)[0].data["status"] == "failed"


async def test_ctrl_c_at_the_prompt_pauses_and_keeps_gate_record_paused(harness: Harness) -> None:
    harness.workflow(
        "t",
        wf("""
  - {id: a, shell: echo a}
  - {id: gate, needs: [a], approve: "ship?"}
  - {id: ship, needs: [gate], shell: echo shipped}
"""),
    )

    async def prompt(request: ApprovalRequest) -> ApprovalAnswer | None:
        os.kill(os.getpid(), signal.SIGINT)  # Ctrl-C while the panel is shown
        await anyio.sleep(30)
        return ApprovalAnswer(True, "")

    result = await harness.run("t", run_id="20260820-000000-cgt", prompt=prompt)
    assert result.status is RunStatus.PAUSED and result.exit_code == 3
    assert result.pause is not None and result.pause.token == "gate#1"
    run = harness.record("20260820-000000-cgt")
    assert run.status is RunStatus.PAUSED and run.pause is not None
    assert run.steps["gate"].status is StepStatus.PAUSED
    assert run.steps["gate"].skip_reason != "interrupted"
    assert "ship" not in run.steps
    finished = [e for e in harness.events(EventType.STEP_FINISHED) if e.step_path == "gate"]
    assert finished and finished[-1].data["status"] == "paused"


async def test_resume_refuses_a_running_run_with_a_live_pid_unless_force(harness: Harness) -> None:
    harness.workflow("t", wf("  - {id: a, shell: echo a}"))
    first = await harness.run("t", run_id="20260820-000000-live")
    assert first.status is RunStatus.SUCCEEDED
    run = harness.record("20260820-000000-live")
    run.status = RunStatus.RUNNING
    run.pid = os.getpid()  # "another process" that is definitely alive
    harness.store.save(run)
    with pytest.raises(ResumeError, match="still running") as info:
        await harness.run("t", resume="20260820-000000-live")
    assert info.value.hint and "rayspec cancel" in info.value.hint and "--force" in info.value.hint
    forced = await harness.run("t", resume="20260820-000000-live", options=RunOptions(force=True))
    assert forced.status is RunStatus.SUCCEEDED
    # a stale pid (process gone) is resumed normally
    run = harness.record("20260820-000000-live")
    run.status = RunStatus.RUNNING
    run.pid = 2**22 - 7  # unlikely to exist
    harness.store.save(run)
    again = await harness.run("t", resume="20260820-000000-live")
    assert again.status is RunStatus.SUCCEEDED


async def test_fsync_backed_store_writes_happen_off_the_event_loop(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    loop_thread = threading.get_ident()
    save_threads: list[int] = []
    output_threads: list[int] = []
    store = harness.store
    real_save, real_write = store.save, store.write_output_with_sha

    def save(run):
        save_threads.append(threading.get_ident())
        return real_save(run)

    def write(*args, **kwargs):
        output_threads.append(threading.get_ident())
        return real_write(*args, **kwargs)

    monkeypatch.setattr(store, "save", save)
    monkeypatch.setattr(store, "write_output_with_sha", write)
    harness.workflow("t", wf("  - {id: a, shell: echo a}\n  - {id: b, needs: [a], shell: echo b}"))
    result = await harness.run("t")
    assert result.status is RunStatus.SUCCEEDED
    assert save_threads and output_threads
    # run.json / output files are fsync'ed: never on the event-loop thread (creation excepted)
    assert all(t != loop_thread for t in save_threads[1:]), save_threads
    assert all(t != loop_thread for t in output_threads)
    assert harness.record(result.run_id).steps["b"].status is StepStatus.SUCCEEDED
