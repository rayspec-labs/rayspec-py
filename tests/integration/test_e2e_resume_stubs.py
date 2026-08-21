"""End to end: ``--stubs`` is recorded in ``run.json`` (``stubs_path``) and reused by
``resume``/``approve``/``reject``; ``rayspec resume --stubs`` overrides; a missing file is exit 2."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ._helpers import invoke, run_records

WORKFLOW = """
rayspec: 1
name: gated
isolation: none
agents:
  r: { provider: stub }
steps:
  - id: gate
    approve: "go?"
  - id: ask
    needs: [gate]
    agent: r
    prompt: "hello"
outputs:
  answer: "{{ steps.ask.output }}"
"""

DRY = """
rayspec: 1
name: dry
isolation: none
agents:
  r: { provider: claude }
steps:
  - id: ask
    agent: r
    prompt: "hello"
    retry: { attempts: 1 }
outputs:
  answer: "{{ steps.ask.output }}"
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / ".rayspec" / "workflows" / "gated.yaml").write_text(textwrap.dedent(WORKFLOW))
    (root / ".rayspec" / "workflows" / "dry.yaml").write_text(textwrap.dedent(DRY))
    (root / "stubs.yaml").write_text("steps:\n  ask: {text: from-stubs}\n")
    (root / "other.yaml").write_text("steps:\n  ask: {text: from-other}\n")
    return root


def _paused(project: Path, home: Path) -> str:
    res = invoke(
        [
            "run",
            "gated",
            "--root",
            str(project),
            "--no-worktree",
            "--no-interactive",
            "--stubs",
            str(project / "stubs.yaml"),
        ],
        home,
    )
    assert res.exit_code == 3, res.output
    (record,) = [r for r in run_records(home) if r["status"] == "paused"]
    assert record["stubs_path"] == str((project / "stubs.yaml").resolve())
    return record["run_id"]


def _record(home: Path, run_id: str) -> dict:
    return next(r for r in run_records(home) if r["run_id"] == run_id)


def test_approve_reuses_the_recorded_stubs(project: Path, home: Path) -> None:
    run_id = _paused(project, home)
    res = invoke(["approve", run_id, "--root", str(project)], home)
    assert res.exit_code == 0, res.output
    assert _record(home, run_id)["outputs"] == {"answer": "from-stubs"}


def test_resume_reuses_overrides_and_refuses_a_missing_file(project: Path, home: Path) -> None:
    run_id = _paused(project, home)
    (project / "stubs.yaml").unlink()
    res = invoke(["resume", run_id, "--root", str(project), "--yes"], home)
    assert res.exit_code == 2, res.output
    assert "stubs" in res.output and "--stubs" in res.output
    assert _record(home, run_id)["status"] == "paused"
    res = invoke(
        ["resume", run_id, "--root", str(project), "--yes", "--stubs", str(project / "other.yaml")],
        home,
    )
    assert res.exit_code == 0, res.output
    record = _record(home, run_id)
    assert record["outputs"] == {"answer": "from-other"}
    assert record["stubs_path"] == str((project / "other.yaml").resolve())


def test_reject_reuses_the_recorded_stubs(project: Path, home: Path) -> None:
    run_id = _paused(project, home)
    res = invoke(["reject", run_id, "--root", str(project)], home)
    assert res.exit_code == 4, res.output  # on_reject: cancel
    # --stubs is accepted on reject too (validated like on run)
    run_id = _paused(project, home)
    res = invoke(["reject", run_id, "--root", str(project), "--stubs", "nope.yaml"], home)
    assert res.exit_code == 2 and "nope.yaml" in res.output, res.output


def test_run_resume_keeps_working_and_falls_back_to_the_record(project: Path, home: Path) -> None:
    run_id = _paused(project, home)
    res = invoke(
        [
            "run",
            "gated",
            "--root",
            str(project),
            "--resume",
            run_id,
            "--yes",
            "--stubs",
            str(project / "other.yaml"),
        ],
        home,
    )
    assert res.exit_code == 0, res.output
    assert _record(home, run_id)["outputs"] == {"answer": "from-other"}
    run_id = _paused(project, home)
    res = invoke(["run", "gated", "--root", str(project), "--resume", run_id, "--yes"], home)
    assert res.exit_code == 0, res.output
    assert _record(home, run_id)["outputs"] == {"answer": "from-stubs"}


def test_a_dry_run_resumes_as_a_dry_run_with_its_stubs(project: Path, home: Path) -> None:
    failing = project / "fail.yaml"
    failing.write_text("steps:\n  ask: {fail: {kind: api, message: boom, transient: false}}\n")
    res = invoke(["run", "dry", "--root", str(project), "--dry-run", "--stubs", str(failing)], home)
    assert res.exit_code == 1, res.output
    run_id = run_records(home)[-1]["run_id"]
    again = invoke(["resume", run_id, "--root", str(project)], home)
    assert again.exit_code == 1, again.output  # the same script fails again — still a dry run
    record = _record(home, run_id)
    assert record["dry_run"] is True and record["status"] == "failed"
    fixed = invoke(
        ["resume", run_id, "--root", str(project), "--stubs", str(project / "stubs.yaml")], home
    )
    assert fixed.exit_code == 0, fixed.output
    record = _record(home, run_id)
    assert record["dry_run"] is True and record["outputs"] == {"answer": "from-stubs"}


def test_run_resume_without_dry_run_names_the_recorded_stubs(project: Path, home: Path) -> None:
    """``run --resume`` keeps its explicit flags: resuming a ``--dry-run --stubs`` record of a
    workflow with a non-stub agent without ``--dry-run`` is refused, and the message says that
    the RECORDED stubs file is the reason (the user never passed ``--stubs``)."""
    failing = project / "fail.yaml"
    failing.write_text("steps:\n  ask: {fail: {kind: api, message: boom, transient: false}}\n")
    res = invoke(["run", "dry", "--root", str(project), "--dry-run", "--stubs", str(failing)], home)
    assert res.exit_code == 1, res.output
    run_id = run_records(home)[-1]["run_id"]
    refused = invoke(["run", "dry", "--root", str(project), "--resume", run_id], home)
    assert refused.exit_code == 2, refused.output
    assert "--dry-run --stubs" in refused.output and str(failing.resolve()) in refused.output
    assert run_id in refused.output and "would run for real" in refused.output
    assert "pass --dry-run" in refused.output
    assert _record(home, run_id)["status"] == "failed"  # nothing was written
    ok = invoke(["run", "dry", "--root", str(project), "--resume", run_id, "--dry-run"], home)
    assert ok.exit_code == 1, ok.output  # the recorded (failing) script drives the dry run again
    assert _record(home, run_id)["dry_run"] is True
