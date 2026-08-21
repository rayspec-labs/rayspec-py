"""The run summary names the worktree, its branch and how to use/clean it."""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from rayspec.cli.commands.run import print_summary
from rayspec.engine.runner import RunResult, Workspace
from rayspec.schema import RunStatus


def _summary(result: RunResult) -> str:
    console = Console(file=io.StringIO(), width=220, force_terminal=False, color_system=None)
    print_summary(console, result, json_mode=False)
    assert isinstance(console.file, io.StringIO)
    return console.file.getvalue()


def _result(workspace: Workspace, status: RunStatus = RunStatus.SUCCEEDED) -> RunResult:
    return RunResult(
        run_id="20260820-101010-abcd",
        status=status,
        exit_code=0 if status is RunStatus.SUCCEEDED else 1,
        run_dir=Path("/home/me/.rayspec/projects/x/runs/20260820-101010-abcd"),
        workspace=workspace,
    )


def test_worktree_run_prints_path_branch_and_hint(tmp_path: Path) -> None:
    wt = tmp_path / "worktrees" / "demo-abcd"
    out = _summary(_result(Workspace(isolation="worktree", workdir=wt, branch="rayspec/demo-abcd")))
    assert f"worktree: {wt}" in out
    assert "branch rayspec/demo-abcd" in out and "checked out there" in out
    # one hint line: how to get in, how to list/clean, how to remove by hand
    assert f"cd {wt}" in out
    assert "rayspec worktrees list|clean" in out
    assert f"git worktree remove {wt}" in out


def test_in_place_run_prints_no_worktree_hint(tmp_path: Path) -> None:
    out = _summary(_result(Workspace.in_place(tmp_path)))
    assert "worktree" not in out and "git worktree remove" not in out
    assert "run dir:" in out


def test_failed_worktree_run_keeps_the_worktree_hint_and_the_resume_hint(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    out = _summary(
        _result(Workspace(isolation="worktree", workdir=wt, branch="rayspec/x-1"), RunStatus.FAILED)
    )
    assert f"cd {wt}" in out and "rayspec resume 20260820-101010-abcd" in out


# -- the cost footer renders the run-level cost source -----------------------------------


def _priced(cost: float | None, source: str) -> RunResult:
    from rayspec.providers.base import Usage

    return RunResult(
        run_id="20260820-101010-abcd",
        status=RunStatus.SUCCEEDED,
        exit_code=0,
        run_dir=Path("/tmp/run"),
        workspace=Workspace.in_place(Path("/tmp/proj")),
        usage=Usage(input=70_000, output=43),
        cost_usd=cost,
        cost_source=source,
    )


def test_cost_footer_marks_partial_table_and_provider_costs() -> None:
    assert "cost: ≥$0.02" in _summary(_priced(0.0158, "partial"))
    assert "cost: ~$0.02" in _summary(_priced(0.0158, "table"))
    assert "cost: $0.02" in _summary(_priced(0.0158, "provider"))
    none = _summary(_priced(None, "none"))
    assert "cost:" not in none and "tokens: 70043" in none


def test_json_summary_carries_the_cost_source() -> None:
    # a --json consumer must be able to tell a lower bound (partial) from a
    # provider-reported sum without opening run.json — additive key, in SUMMARY_KEYS
    import io
    import json

    from rich.console import Console

    from rayspec.cli.commands.run import SUMMARY_KEYS

    console = Console(file=io.StringIO(), width=220, force_terminal=False, color_system=None)
    print_summary(console, _priced(0.0158, "partial"), json_mode=True)
    assert isinstance(console.file, io.StringIO)
    payload = json.loads(console.file.getvalue())
    assert payload["cost_source"] == "partial" and payload["cost_usd"] == 0.0158
    assert "cost_source" in SUMMARY_KEYS and set(payload) == SUMMARY_KEYS


def test_tokens_footer_marks_unknown_usage_of_interrupted_attempts() -> None:
    # an attempt cut off before any usage report makes the token total a lower bound
    from rayspec.store.model import StepRecord

    result = _priced(None, "none")
    result.steps = {
        "think": StepRecord(path="think", id="think", kind="prompt", usage_unknown=True),
        "ok": StepRecord(path="ok", id="ok", kind="prompt"),
    }
    out = _summary(result)
    assert "tokens: ≥70043" in out and "usage of 1 step unknown" in out
    assert "tokens: 70043" in _summary(_priced(None, "none"))
    from rayspec.providers.base import Usage

    result.usage = Usage()
    assert "tokens: unknown (usage of 1 step unknown)" in _summary(result)


# -- the failure hint names the failed LEAF step path, never a composite -------------------


def _failed_result(steps: dict[str, tuple[str, str]], reason: str) -> RunResult:
    """``steps``: path -> (kind, status)."""
    from rayspec.schema import StepStatus
    from rayspec.store.model import StepRecord

    records = {
        path: StepRecord(
            path=path, id=path.rsplit("/", 1)[-1], kind=kind, status=StepStatus(status)
        )
        for path, (kind, status) in steps.items()
    }
    return RunResult(
        run_id="20260820-101010-abcd",
        status=RunStatus.FAILED,
        exit_code=1,
        run_dir=Path("/tmp/run"),
        workspace=Workspace.in_place(Path("/tmp/proj")),
        reason=reason,
        steps=records,
    )


def test_failure_hint_names_the_failed_leaf_not_the_loop() -> None:
    out = _summary(
        _failed_result(
            {
                "build": ("loop", "failed"),
                "build[3]/implement": ("prompt", "succeeded"),
                "build[3]/check": ("shell", "failed"),
            },
            "step 'build' failed: iteration 3: step 'check' failed: exit code 1",
        )
    )
    assert "--step build[3]/check" in out and "--step build " not in out


def test_failure_hint_prefers_the_step_the_reason_names_and_counts_the_rest() -> None:
    out = _summary(
        _failed_result(
            {
                "tests": ("shell", "failed"),
                "meta": ("shell", "failed"),
                "report": ("prompt", "skipped"),
            },
            "step 'meta' failed: stdout is not valid JSON",
        )
    )
    assert "rayspec logs 20260820-101010-abcd --step meta (+1 more)" in out


def test_failure_hint_without_a_failed_leaf_drops_the_step_flag() -> None:
    out = _summary(_failed_result({"gate": ("approve", "rejected")}, "rejected at gate"))
    assert "rayspec logs 20260820-101010-abcd ·" in out and "--step" not in out
