from __future__ import annotations

import json

from rayspec.providers.base import Usage
from rayspec.schema import RunStatus, StepStatus
from rayspec.store.model import (
    RUN_RECORD_SCHEMA_VERSION,
    Decision,
    ErrorInfo,
    PauseInfo,
    RunRecord,
    SessionRef,
    StepRecord,
    WorkspaceInfo,
    new_run_id,
)


def test_new_run_id_is_time_sortable_and_unique():
    a = new_run_id()
    b = new_run_id()
    assert a != b
    assert len(a.split("-")) == 3
    assert a[:8].isdigit() and a[9:15].isdigit()


def test_step_record_defaults():
    rec = StepRecord(path="assess", id="assess", kind="prompt")
    assert rec.status is StepStatus.PENDING and rec.attempts == 0
    assert rec.output_ref is None and rec.tolerated is False and rec.usage == Usage()
    assert rec.reusable is False


def test_step_record_reusable_rules():
    ok = StepRecord(
        path="a", id="a", kind="shell", status=StepStatus.SUCCEEDED, output_ref="steps/a/output.txt"
    )
    assert ok.reusable
    no_output = StepRecord(path="a", id="a", kind="shell", status=StepStatus.SUCCEEDED)
    assert not no_output.reusable
    tolerated = StepRecord(
        path="a", id="a", kind="shell", status=StepStatus.FAILED, tolerated=True, output_ref="x"
    )
    assert tolerated.reusable
    failed = StepRecord(path="a", id="a", kind="shell", status=StepStatus.FAILED, output_ref="x")
    assert not failed.reusable


def test_run_record_roundtrips_through_json():
    run = RunRecord(
        run_id=new_run_id(),
        workflow_name="fix_issue",
        workflow_path="/repo/.rayspec/workflows/fix_issue.yaml",
        workflow_hash="sha256:abc",
        project_slug="github.com/o/r",
        project_root="/repo",
        inputs={"issue": 12},
        workspace=WorkspaceInfo(
            isolation="worktree", workdir="/wt", branch="rayspec/fix_issue-ab12", base_branch="main"
        ),
    )
    run.steps["assess"] = StepRecord(
        path="assess",
        id="assess",
        kind="prompt",
        status=StepStatus.SUCCEEDED,
        attempts=1,
        output_ref="steps/assess/output.json",
        session_ref=SessionRef(provider="claude", id="sess-1"),
        usage=Usage(input=10, output=5),
        cost_usd=0.01,
    )
    run.pause = PauseInfo(
        token="confirm#1", step="confirm", message="ok?", decision=Decision(approved=True, by="cli")
    )
    run.steps["x"] = StepRecord(
        path="x",
        id="x",
        kind="shell",
        status=StepStatus.FAILED,
        error=ErrorInfo(type="shell", message="exit 1"),
    )
    data = json.loads(run.model_dump_json())
    assert data["schema"] == RUN_RECORD_SCHEMA_VERSION
    back = RunRecord.model_validate(data)
    assert back == run
    assert back.status is RunStatus.RUNNING
    assert back.steps["assess"].usage.total == 15
    assert (
        back.pause is not None and back.pause.decision is not None and back.pause.decision.approved
    )


def test_run_record_totals():
    run = RunRecord(
        run_id="r",
        workflow_name="w",
        workflow_path="p",
        workflow_hash="h",
        project_slug="s",
        project_root="/",
    )
    run.steps["a"] = StepRecord(
        path="a", id="a", kind="prompt", usage=Usage(input=1, output=2), cost_usd=0.5
    )
    run.steps["b"] = StepRecord(path="b", id="b", kind="prompt", usage=Usage(input=3, output=4))
    run.steps["c"] = StepRecord(path="c", id="c", kind="shell")
    assert run.total_usage() == Usage(input=4, output=6)
    assert run.total_cost_usd() == 0.5
