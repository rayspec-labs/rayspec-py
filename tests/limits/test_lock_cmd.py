"""``rayspec lock`` and the ``--locked`` gate on ``run`` / ``plan`` / ``validate``."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from rayspec.cli.app import app
from rayspec.limits import lockfile_path

runner = CliRunner()

WORKFLOW = """\
rayspec: 1
name: t
agents:
  reviewer:
    provider: claude
    model: {model}
steps:
  - {{id: a, prompt: "hi", agent: reviewer}}
"""


@pytest.fixture
def root(tmp_path: Path, home: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".rayspec" / "workflows").mkdir(parents=True)
    (project / ".rayspec" / "workflows" / "t.yaml").write_text(
        WORKFLOW.format(model="claude-sonnet-4-6"), encoding="utf-8"
    )
    return project


def drift(root: Path) -> None:
    (root / ".rayspec" / "workflows" / "t.yaml").write_text(
        WORKFLOW.format(model="claude-opus-4-9"), encoding="utf-8"
    )


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


def test_lock_writes_the_file_and_check_is_then_quiet(root: Path) -> None:
    result = invoke("lock", "--root", str(root))
    assert result.exit_code == 0, result.output
    assert lockfile_path(root).exists()
    assert "rayspec.lock" in result.output
    checked = invoke("lock", "--check", "--root", str(root))
    assert checked.exit_code == 0, checked.output
    assert "up to date" in checked.output


def test_lock_check_reports_drift_and_exits_one(root: Path) -> None:
    invoke("lock", "--root", str(root))
    drift(root)
    result = invoke("lock", "--check", "--root", str(root))
    assert result.exit_code == 1
    assert "agents.reviewer" in result.output
    assert "claude-sonnet-4-6" in result.output and "claude-opus-4-9" in result.output


def test_lock_json_lists_the_pins(root: Path) -> None:
    result = invoke("lock", "--json", "--root", str(root))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["path"].endswith("rayspec.lock")
    assert payload["workflows"]["t"]["agents.reviewer"]["model"] == "claude-sonnet-4-6"
    assert payload["drift"] == []


def test_locking_one_workflow_keeps_the_others(root: Path) -> None:
    (root / ".rayspec" / "workflows" / "u.yaml").write_text(
        WORKFLOW.format(model="claude-haiku-4-1").replace("name: t", "name: u"), encoding="utf-8"
    )
    assert invoke("lock", "--root", str(root)).exit_code == 0
    assert invoke("lock", "t", "--root", str(root)).exit_code == 0
    payload = json.loads(invoke("lock", "--json", "--root", str(root)).stdout)
    assert set(payload["workflows"]) == {"t", "u"}


def test_lock_names_an_agent_it_cannot_pin(root: Path) -> None:
    (root / ".rayspec" / "workflows" / "t.yaml").write_text(
        textwrap.dedent(
            """\
            rayspec: 1
            name: t
            steps:
              - {id: a, prompt: "hi", agent: {provider: mystery}}
            """
        ),
        encoding="utf-8",
    )
    result = invoke("lock", "--root", str(root))
    assert result.exit_code == 0, result.output
    assert "no literal model id" in result.output


def test_lock_refuses_an_unknown_workflow(root: Path) -> None:
    result = invoke("lock", "nope", "--root", str(root))
    assert result.exit_code == 2
    assert "unknown workflow" in result.output


# -- the --locked gate --------------------------------------------------------------------------


def test_run_locked_refuses_a_drifted_agent(root: Path) -> None:
    invoke("lock", "--root", str(root))
    drift(root)
    result = invoke("run", "t", "--dry-run", "--locked", "--root", str(root))
    assert result.exit_code == 2
    assert "agents.reviewer" in result.output
    assert "claude-sonnet-4-6" in result.output and "claude-opus-4-9" in result.output


def test_run_without_locked_still_runs_a_drifted_workflow(root: Path) -> None:
    invoke("lock", "--root", str(root))
    drift(root)
    result = invoke("run", "t", "--dry-run", "--root", str(root))
    assert result.exit_code == 0, result.output


def test_run_locked_without_a_lockfile_points_at_rayspec_lock(root: Path) -> None:
    result = invoke("run", "t", "--dry-run", "--locked", "--root", str(root))
    assert result.exit_code == 2
    assert "rayspec lock" in result.output


def test_locked_is_on_by_default_under_ci(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    invoke("lock", "--root", str(root))
    drift(root)
    monkeypatch.setenv("CI", "true")
    assert invoke("run", "t", "--dry-run", "--root", str(root)).exit_code == 2
    # ... and --no-locked opts out again
    assert invoke("run", "t", "--dry-run", "--no-locked", "--root", str(root)).exit_code == 0


def test_plan_and_validate_enforce_the_lockfile_too(root: Path) -> None:
    invoke("lock", "--root", str(root))
    drift(root)
    planned = invoke("plan", "t", "--locked", "--root", str(root))
    assert planned.exit_code == 2 and "agents.reviewer" in planned.output
    validated = invoke("validate", "t", "--locked", "--root", str(root))
    assert validated.exit_code == 2 and "agents.reviewer" in validated.output
    assert invoke("validate", "t", "--root", str(root)).exit_code == 0


def test_a_malformed_lockfile_is_a_usage_error(root: Path) -> None:
    lockfile_path(root).write_text("workflows: 3\n", encoding="utf-8")
    result = invoke("run", "t", "--dry-run", "--locked", "--root", str(root))
    assert result.exit_code == 2
    assert "workflows" in result.output


def test_the_ci_default_leaves_a_project_without_a_lockfile_alone(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default must not break a project that never opted in — only the flag promises."""
    monkeypatch.setenv("CI", "true")
    assert invoke("run", "t", "--dry-run", "--root", str(root)).exit_code == 0
    assert invoke("validate", "t", "--root", str(root)).exit_code == 0
    assert invoke("plan", "t", "--root", str(root)).exit_code == 0
    # an explicitly passed --locked still refuses a missing lockfile: that IS the promise
    explicit = invoke("run", "t", "--dry-run", "--locked", "--root", str(root))
    assert explicit.exit_code == 2 and "no lockfile" in explicit.output


def _git_init(path: Path) -> None:
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(args, cwd=path, check=True)


def test_repo_checks_the_lockfile_of_the_repo_it_runs(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``--repo`` the workflow comes from the target — so must the lockfile."""
    target = tmp_path / "target"
    (target / ".rayspec" / "workflows").mkdir(parents=True)
    (target / ".rayspec" / "workflows" / "t.yaml").write_text(
        WORKFLOW.format(model="claude-sonnet-4-6"), encoding="utf-8"
    )
    assert invoke("lock", "--root", str(target)).exit_code == 0
    drift(target)  # the target's workflow no longer matches its own lockfile
    _git_init(target)

    caller = tmp_path / "caller"
    (caller / ".rayspec" / "workflows").mkdir(parents=True)
    (caller / ".rayspec" / "workflows" / "t.yaml").write_text(
        WORKFLOW.format(model="claude-opus-4-9"), encoding="utf-8"
    )
    assert invoke("lock", "--root", str(caller)).exit_code == 0  # pins the DRIFTED model

    monkeypatch.setenv("CI", "true")
    result = invoke(
        "run", "t", "--dry-run", "--repo", str(target), "--root", str(caller), "--no-interactive"
    )
    assert result.exit_code == 2, result.output
    assert "agents.reviewer" in result.output
