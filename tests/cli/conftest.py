"""Fixtures for the run-management CLI tests: a temp RAYSPEC_HOME seeded with run records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.commands.run import project_slug_for
from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.providers.base import Usage
from rayspec.schema import RunStatus, StepStatus
from rayspec.store.file import FileRunStore
from rayspec.store.model import (
    ErrorInfo,
    LoopInfo,
    PauseInfo,
    RunRecord,
    StepRecord,
    WorkspaceInfo,
)

SUCCEEDED_ID = "20260820-100000-aaaa"
FAILED_ID = "20260820-110000-bbbb"
PAUSED_ID = "20260820-120000-abcd"
OTHER_ID = "20260819-090000-zzzz"
OTHER_SLUG = "local/other-deadbeef"

T0 = datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC)


@dataclass
class Seeded:
    """What the seeded home contains."""

    home: Path
    project: Path
    slug: str
    store: FileRunStore
    other_store: FileRunStore


def _step(
    path: str,
    kind: str,
    status: StepStatus = StepStatus.SUCCEEDED,
    *,
    start: datetime,
    duration_ms: int = 1000,
    usage: Usage | None = None,
    cost_usd: float | None = None,
    cost_source: str = "none",
    error: ErrorInfo | None = None,
    attempts: int = 1,
    **extra: object,
) -> StepRecord:
    ended = start + timedelta(milliseconds=duration_ms)
    terminal = status not in {StepStatus.PENDING, StepStatus.RUNNING, StepStatus.PAUSED}
    return StepRecord(
        path=path,
        id=path.rsplit("/", 1)[-1].split("[", 1)[0],
        kind=kind,
        status=status,
        attempts=attempts,
        started_at=start,
        ended_at=ended if terminal else None,
        duration_ms=duration_ms if terminal else None,
        ok=(status is StepStatus.SUCCEEDED) if terminal else None,
        usage=usage or Usage(),
        cost_usd=cost_usd,
        cost_source=cost_source,
        error=error,
        **extra,  # type: ignore[arg-type]
    )


def _seed_succeeded(store: FileRunStore, slug: str, root: Path) -> None:
    run = RunRecord(
        run_id=SUCCEEDED_ID,
        workflow_name="fixit",
        workflow_path=".rayspec/workflows/fixit.yaml",
        workflow_hash="a" * 64,
        project_slug=slug,
        project_root=str(root),
        inputs={"issue": 7},
        status=RunStatus.SUCCEEDED,
        created_at=T0,
        started_at=T0,
        ended_at=T0 + timedelta(seconds=95),
        workspace=WorkspaceInfo(
            isolation="worktree",
            workdir=str(root / "wt"),
            branch="rayspec/fixit-aaaa",
            base_branch="main",
            base_sha="1234567890abcdef",
            head_sha="fedcba0987654321",
        ),
        outputs={"verdict": "fix", "summary": {"files": 2}},
    )
    store.create(run)
    t = T0
    store.record_step(run, _step("fetch", "shell", start=t, duration_ms=850), "issue 7\n")
    t += timedelta(seconds=1)
    store.record_step(
        run,
        _step(
            "assess",
            "prompt",
            start=t,
            duration_ms=12_300,
            usage=Usage(input=1200, output=300),
            cost_usd=0.0456,
            cost_source="provider",
            provider="stub",
            model="stub-1",
        ),
        '{"verdict": "fix", "reason": "real bug"}',
        kind="json",
    )
    t += timedelta(seconds=13)
    store.record_step(
        run,
        _step(
            "build[1]/implement",
            "prompt",
            start=t,
            duration_ms=60_000,
            usage=Usage(input=5000, output=2000),
            cost_usd=0.21,
            cost_source="table",
            provider="stub",
            model="stub-1",
            iteration=1,
        ),
        "patched the thing\nline two\nline three",
    )
    store.record_step(
        run,
        _step("build[1]/check", "shell", start=t + timedelta(seconds=61), duration_ms=400),
        "ok\n",
    )
    store.record_step(
        run,
        _step(
            "build",
            "loop",
            start=t,
            duration_ms=62_000,
            loop=LoopInfo(iterations=1, converged=True),
        ),
        '{"implement": "patched the thing", "check": "ok"}',
        kind="json",
    )
    for idx, (etype, path, data) in enumerate(
        [
            (EventType.RUN_STARTED, None, {"workflow": "fixit", "dry_run": False}),
            (EventType.STEP_STARTED, "fetch", {"kind": "shell", "attempt": 1}),
            (
                EventType.STEP_FINISHED,
                "fetch",
                {"status": "succeeded", "duration_ms": 850, "usage": {}, "cost_usd": None},
            ),
            (EventType.STEP_STARTED, "assess", {"kind": "prompt", "attempt": 1}),
            (
                EventType.STEP_FINISHED,
                "assess",
                {
                    "status": "succeeded",
                    "duration_ms": 12300,
                    "usage": {"input": 1200, "output": 300},
                    "cost_usd": 0.0456,
                },
            ),
            (EventType.LOOP_ITERATION, "build", {"n": 1, "max": 3}),
            (EventType.STEP_FINISHED, "build[1]/implement", {"status": "succeeded"}),
            (
                EventType.RUN_FINISHED,
                None,
                {"status": "succeeded", "reason": None, "usage": {}, "cost_usd": 0.2556},
            ),
        ]
    ):
        store.append_event(
            run.run_id,
            RunEvent(
                type=etype,
                run_id=run.run_id,
                ts=T0 + timedelta(seconds=idx),
                step_path=path,
                data=data,
            ),
        )
    for _idx, rec in enumerate(
        [
            StreamRecord(kind="session", text="stub:assess:1", ts=T0 + timedelta(seconds=1)),
            StreamRecord(kind="text_delta", text="looking", ts=T0 + timedelta(seconds=2)),
            StreamRecord(
                kind="tool_call",
                name="Bash",
                call_id="c1",
                text="",
                data={"input": {"cmd": "ls"}},
                ts=T0 + timedelta(seconds=3),
            ),
            StreamRecord(
                kind="tool_result", call_id="c1", text="a.py", ts=T0 + timedelta(seconds=4)
            ),
            StreamRecord(kind="text", text="looking good", ts=T0 + timedelta(seconds=5)),
            StreamRecord(
                kind="usage",
                data={"usage": {"input": 1200, "output": 300}},
                ts=T0 + timedelta(seconds=6),
            ),
        ]
    ):
        store.append_stream(run.run_id, "assess", rec)
    for rec in [
        StreamRecord(kind="stdout", text="issue 7\n"),
        StreamRecord(kind="exit", text="0", data={"exit_code": 0}),
    ]:
        store.append_stream(run.run_id, "fetch", rec)


def _seed_failed(store: FileRunStore, slug: str, root: Path) -> None:
    start = T0 + timedelta(hours=1)
    run = RunRecord(
        run_id=FAILED_ID,
        workflow_name="deploy",
        workflow_path=".rayspec/workflows/deploy.yaml",
        workflow_hash="b" * 64,
        project_slug=slug,
        project_root=str(root),
        status=RunStatus.FAILED,
        reason="step 'b' failed: exit: exit code 1",
        created_at=start,
        started_at=start,
        ended_at=start + timedelta(seconds=3),
        workspace=WorkspaceInfo(isolation="none", workdir=str(root)),
    )
    store.create(run)
    store.record_step(run, _step("a", "shell", start=start, duration_ms=200), "a\n")
    store.record_step(
        run,
        _step(
            "b",
            "shell",
            StepStatus.FAILED,
            start=start + timedelta(seconds=1),
            duration_ms=100,
            error=ErrorInfo(type="exit", message="exit code 1"),
            exit_code=1,
        ),
        "",
    )
    run.steps["c"] = StepRecord(
        path="c", id="c", kind="shell", status=StepStatus.SKIPPED, skip_reason="upstream_failed"
    )
    store.save(run)
    store.append_event(
        run.run_id,
        RunEvent(type=EventType.RUN_STARTED, run_id=run.run_id, data={"workflow": "deploy"}),
    )
    store.append_event(
        run.run_id,
        RunEvent(
            type=EventType.STEP_FINISHED,
            run_id=run.run_id,
            step_path="b",
            data={"status": "failed", "error": {"type": "exit", "message": "exit code 1"}},
        ),
    )
    store.append_event(
        run.run_id,
        RunEvent(
            type=EventType.RUN_FINISHED,
            run_id=run.run_id,
            data={"status": "failed", "reason": run.reason},
        ),
    )


def _seed_paused(store: FileRunStore, slug: str, root: Path) -> None:
    start = T0 + timedelta(hours=2)
    run = RunRecord(
        run_id=PAUSED_ID,
        workflow_name="gate",
        workflow_path=".rayspec/workflows/gate.yaml",
        workflow_hash="c" * 64,
        project_slug=slug,
        project_root=str(root),
        status=RunStatus.PAUSED,
        reason="awaiting approval at ok",
        created_at=start,
        started_at=start,
        ended_at=start + timedelta(seconds=2),
        pid=4242,
        host="nowhere",
        workspace=WorkspaceInfo(isolation="none", workdir=str(root)),
        pause=PauseInfo(
            token="ok#1", step="ok", message="ship it?", requested_at=start + timedelta(seconds=2)
        ),
    )
    store.create(run)
    store.record_step(run, _step("a", "shell", start=start, duration_ms=300), "a\n")
    run.steps["ok"] = _step(
        "ok", "approve", StepStatus.PAUSED, start=start + timedelta(seconds=1), attempts=1
    )
    store.save(run)
    store.append_event(
        run.run_id,
        RunEvent(
            type=EventType.RUN_PAUSED,
            run_id=run.run_id,
            step_path="ok",
            data={"token": "ok#1", "step": "ok", "message": "ship it?"},
        ),
    )


def _seed_other(store: FileRunStore, root: Path) -> None:
    start = T0 - timedelta(days=1)
    run = RunRecord(
        run_id=OTHER_ID,
        workflow_name="other",
        workflow_path=".rayspec/workflows/other.yaml",
        workflow_hash="d" * 64,
        project_slug=OTHER_SLUG,
        project_root=str(root / "other"),
        status=RunStatus.SUCCEEDED,
        created_at=start,
        started_at=start,
        ended_at=start + timedelta(seconds=1),
        outputs={},
    )
    store.create(run)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    return root


@pytest.fixture
def seeded(home: Path, project: Path) -> Seeded:
    slug = project_slug_for(project)
    store = FileRunStore(home / "projects" / slug)
    _seed_succeeded(store, slug, project)
    _seed_failed(store, slug, project)
    _seed_paused(store, slug, project)
    other = FileRunStore(home / "projects" / OTHER_SLUG)
    _seed_other(other, project)
    return Seeded(home=home, project=project, slug=slug, store=store, other_store=other)


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()
