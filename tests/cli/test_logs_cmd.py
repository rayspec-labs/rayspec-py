"""`rayspec logs <run> [--step <path>] [--follow] [--stream] [--json]`."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands import logs as logs_mod
from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.schema import RunStatus
from rayspec.store.model import RunRecord

from .conftest import FAILED_ID, PAUSED_ID, SUCCEEDED_ID, Seeded

pytestmark = pytest.mark.anyio


def test_logs_renders_events(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["logs", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 8
    assert "10:00:00" in lines[0] and "started" in lines[0] and "fixit" in lines[0]
    assert "fetch" in lines[1] and "shell" in lines[1]  # step.started shown
    assert "assess" in lines[4] and "succeeded" in lines[4] and "12.3s" in lines[4]
    assert "loop.iteration" in lines[5] and "build" in lines[5]
    assert "run" in lines[-1] and "succeeded" in lines[-1]


def test_logs_step_renders_stream(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "assess", "--root", str(seeded.project)]
    )
    assert result.exit_code == 0, result.output
    out = result.output
    assert "session" in out and "stub:assess:1" in out
    assert "looking good" in out
    assert "Bash" in out and '"cmd": "ls"' in out and "a.py" in out
    assert "usage" in out
    shell = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "fetch", "--root", str(seeded.project)]
    )
    assert shell.exit_code == 0, shell.output
    assert "issue 7" in shell.output and "exit 0" in shell.output
    missing = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "nope", "--root", str(seeded.project)]
    )
    assert missing.exit_code == 2 and "no step 'nope'" in missing.output
    bad = cli.invoke(app, ["logs", SUCCEEDED_ID, "--step", "", "--root", str(seeded.project)])
    assert bad.exit_code == 2


def test_logs_stream_interleaves_all_steps(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["logs", SUCCEEDED_ID, "--stream", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert any("[assess]" in line and "looking good" in line for line in lines)
    assert any("[fetch]" in line and "issue 7" in line for line in lines)
    # ordered by timestamp: run.started (10:00:00) first, the assess session (10:00:01) after it
    first_event = next(i for i, line in enumerate(lines) if "started" in line)
    session = next(i for i, line in enumerate(lines) if "stub:assess:1" in line)
    assert first_event < session


def test_logs_json_emits_raw_jsonl(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["logs", SUCCEEDED_ID, "--json", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.output.splitlines()]
    assert [r["type"] for r in rows][:2] == ["run.started", "step.started"]
    assert rows[0]["run_id"] == SUCCEEDED_ID
    step = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "assess", "--json", "--root", str(seeded.project)]
    )
    rows = [json.loads(line) for line in step.output.splitlines()]
    assert rows[0] == {
        "type": "stream",
        "step_path": "assess",
        "record": json.loads(StreamRecord.model_validate(rows[0]["record"]).model_dump_json()),
    }
    assert rows[0]["record"]["kind"] == "session"


def test_logs_prefix_and_failed_run(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["logs", FAILED_ID[:11], "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "exit code 1" in result.output and "failed" in result.output


# -- follow -------------------------------------------------------------------------------------


def _running_run(seeded: Seeded, run_id: str) -> RunRecord:
    # PRD-07 R4: `reconcile_run` treats a `running` record with no live, recently-heartbeating
    # pid as interrupted — this process's own pid (always alive) and a fresh heartbeat keep the
    # simulated run "live" for the reconciliation check, same as a real run would be.
    run = RunRecord(
        run_id=run_id,
        workflow_name="live",
        workflow_path="x.yaml",
        workflow_hash="e" * 64,
        project_slug=seeded.slug,
        project_root=str(seeded.project),
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC),
        pid=os.getpid(),
        heartbeat_at=datetime.now(UTC),
    )
    seeded.store.create(run)
    return run


async def test_follow_tails_until_the_run_finishes(seeded: Seeded) -> None:
    run = _running_run(seeded, "20260820-130000-live")
    store = seeded.store
    seen: list[str] = []

    async def writer() -> None:
        base = datetime.now(UTC)
        store.append_event(
            run.run_id, RunEvent(type=EventType.RUN_STARTED, run_id=run.run_id, ts=base)
        )
        await anyio.sleep(0.05)
        store.append_event(
            run.run_id,
            RunEvent(
                type=EventType.STEP_STARTED,
                run_id=run.run_id,
                step_path="a",
                ts=base + timedelta(milliseconds=1),
                data={"kind": "shell", "attempt": 1},
            ),
        )
        store.append_stream(run.run_id, "a", StreamRecord(kind="stdout", text="hello\n"))
        await anyio.sleep(0.05)
        store.append_event(
            run.run_id,
            RunEvent(
                type=EventType.STEP_FINISHED,
                run_id=run.run_id,
                step_path="a",
                ts=base + timedelta(milliseconds=2),
                data={"status": "succeeded", "duration_ms": 5},
            ),
        )
        run.status = RunStatus.SUCCEEDED
        run.ended_at = datetime.now(UTC)
        store.save(run)
        store.append_event(
            run.run_id,
            RunEvent(
                type=EventType.RUN_FINISHED,
                run_id=run.run_id,
                ts=base + timedelta(milliseconds=3),
                data={"status": "succeeded"},
            ),
        )

    async with anyio.create_task_group() as tg:
        tg.start_soon(writer)
        with anyio.fail_after(5):
            await logs_mod.follow(
                store,
                run.run_id,
                step=None,
                stream=True,
                emit=lambda source, item: seen.append(f"{source}:{item.__class__.__name__}"),
                poll_s=0.01,
            )
    assert seen == [
        "events:RunEvent",
        "events:RunEvent",
        "a:StreamRecord",
        "events:RunEvent",
        "events:RunEvent",
    ]


def test_logs_follow_cli_with_background_writer(cli: CliRunner, seeded: Seeded) -> None:
    run = _running_run(seeded, "20260820-140000-tail")
    store = seeded.store

    def writer() -> None:
        time.sleep(0.1)
        store.append_event(
            run.run_id,
            RunEvent(type=EventType.RUN_STARTED, run_id=run.run_id, data={"workflow": "live"}),
        )
        time.sleep(0.1)
        store.append_stream(run.run_id, "a", StreamRecord(kind="text", text="hi from a"))
        # closing event first, terminal status second — deliberately NOT the engine's order.
        # A reader that sees the terminal status does one last drain and stops; if the status
        # landed first, the event could still be unwritten and the drain would legitimately
        # miss it, so the assertions below would be timing-dependent rather than about
        # `--follow`. The event still arrives after the reader's first poll, so the drain is
        # exercised either way. Do not "restore" this to mirror the engine.
        store.append_event(
            run.run_id,
            RunEvent(
                type=EventType.RUN_FINISHED,
                run_id=run.run_id,
                data={"status": "failed", "reason": "boom"},
            ),
        )
        run.status = RunStatus.FAILED
        run.reason = "boom"
        store.save(run)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        result = cli.invoke(
            app, ["logs", run.run_id, "--follow", "--stream", "--root", str(seeded.project)]
        )
    finally:
        thread.join(timeout=5)
    assert result.exit_code == 0, result.output
    assert "started" in result.output
    assert "hi from a" in result.output
    assert "boom" in result.output
    # a finished run: --follow returns immediately after printing everything
    again = cli.invoke(app, ["logs", run.run_id, "--follow", "--root", str(seeded.project)])
    assert again.exit_code == 0 and "boom" in again.output


def test_logs_step_validation(cli: CliRunner, seeded: Seeded) -> None:
    empty = cli.invoke(app, ["logs", SUCCEEDED_ID, "--step", "", "--root", str(seeded.project)])
    assert empty.exit_code == 2 and "--step needs a step path" in empty.output
    bad = cli.invoke(app, ["logs", SUCCEEDED_ID, "--step", "a b", "--root", str(seeded.project)])
    assert bad.exit_code == 2 and "invalid step path" in bad.output
    unknown = cli.invoke(
        app, ["logs", SUCCEEDED_ID, "--step", "nope", "--root", str(seeded.project)]
    )
    assert unknown.exit_code == 2 and "no step 'nope'" in unknown.output


def test_exit_code_requires_follow(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["logs", SUCCEEDED_ID, "--exit-code", "--root", str(seeded.project)])
    assert result.exit_code == 2
    assert "needs --follow" in result.output


def test_follow_exit_code_returns_the_runs_code(cli: CliRunner, seeded: Seeded) -> None:
    """A finished run followed with --exit-code exits with its status code: 0/1/3 here."""
    for run_id, code in ((SUCCEEDED_ID, 0), (FAILED_ID, 1), (PAUSED_ID, 3)):
        result = cli.invoke(
            app, ["logs", run_id, "--follow", "--exit-code", "--root", str(seeded.project)]
        )
        assert result.exit_code == code, (run_id, result.output)


def test_follow_without_exit_code_exits_zero_on_a_failed_run(
    cli: CliRunner, seeded: Seeded
) -> None:
    result = cli.invoke(app, ["logs", FAILED_ID, "--follow", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output


def test_follow_does_not_append_an_outputs_line(cli: CliRunner, seeded: Seeded) -> None:
    """PRD-07 D20: --follow renders run.finished exactly as plain logs — no extra ` outputs:`
    line that a reader of the plain log would not see (`rayspec show` has the outputs)."""
    plain = cli.invoke(app, ["logs", SUCCEEDED_ID, "--root", str(seeded.project)])
    followed = cli.invoke(app, ["logs", SUCCEEDED_ID, "--follow", "--root", str(seeded.project)])
    assert plain.exit_code == 0 and followed.exit_code == 0
    assert "outputs:" not in followed.output
    assert "verdict" not in followed.output  # the outputs values are not spilled into the log line
