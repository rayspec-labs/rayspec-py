"""The operational limits apply to the second half of a run too.

``resume`` / ``approve`` / ``reject`` are how an unattended job continues a paused run — the
single most common CI shape once an ``approve:`` gate exists. A ceiling that only ``rayspec run``
honours is a ceiling with a hole in it.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from rayspec.cli.app import app
from rayspec.limits import LimitsPolicy, RunSlot

runner = CliRunner()

WORKFLOW = """\
rayspec: 1
name: t
agents:
  reviewer:
    provider: stub
    model: medium
steps:
  - {id: gate, approve: "ok?"}
  - {id: a, needs: [gate], prompt: "hi", agent: reviewer}
"""


def write_config(root: Path, model: str) -> None:
    (root / ".rayspec" / "config.yaml").write_text(
        textwrap.dedent(f"""\
            tiers:
              stub:
                medium: {{model: {model}}}
            """),
        encoding="utf-8",
    )


@pytest.fixture
def root(tmp_path: Path, home: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".rayspec" / "workflows").mkdir(parents=True)
    (project / ".rayspec" / "workflows" / "t.yaml").write_text(WORKFLOW, encoding="utf-8")
    write_config(project, "m1")
    return project


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


def paused_run(root: Path) -> str:
    """Run ``t`` until its gate and return the run id."""
    result = invoke("run", "t", "--no-interactive", "--json", "--root", str(root))
    assert result.exit_code == 3, result.output
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    return str(summary["run_id"])


def test_approve_refuses_a_model_that_drifted_since_the_gate(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workflow file is untouched — only what its tier resolves to moved."""
    run_id = paused_run(root)
    assert invoke("lock", "--root", str(root)).exit_code == 0
    write_config(root, "m2")
    monkeypatch.setenv("CI", "true")
    result = invoke("approve", run_id, "--root", str(root))
    assert result.exit_code == 2, result.output
    assert "agents.reviewer" in result.output and "m1" in result.output


def test_resume_takes_a_run_slot_like_run_does(
    root: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = paused_run(root)
    monkeypatch.setattr(
        "rayspec.limits.limits_policy",
        lambda *_a, **_k: LimitsPolicy(max_concurrent_runs={"stub": 1}),
    )
    held = RunSlot(home, "stub", 1, run_id="holder").acquire()
    try:
        result = invoke("approve", run_id, "--root", str(root))
        assert result.exit_code == 2, result.output
        assert "stub run slot" in result.output and "holder" in result.output
    finally:
        held.release()
    # once the slot is free the same command goes through
    assert invoke("approve", run_id, "--root", str(root)).exit_code == 0


def test_resume_can_queue_for_a_slot(root: Path, home: Path, monkeypatch) -> None:
    run_id = paused_run(root)
    monkeypatch.setattr(
        "rayspec.limits.limits_policy",
        lambda *_a, **_k: LimitsPolicy(max_concurrent_runs={"stub": 1}),
    )
    held = RunSlot(home, "stub", 1, run_id="holder").acquire()
    try:
        result = invoke("resume", run_id, "--yes", "--wait-slot", "1s", "--root", str(root))
        assert result.exit_code == 2, result.output
        assert "after waiting" in result.output
    finally:
        held.release()
