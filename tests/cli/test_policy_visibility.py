"""Every command that loads a workflow says which policy layers it read.

A guardrail nobody can see is a guardrail nobody can trust: policy is discovered against
``--root`` rather than against the workflow file, so "did my policy.yaml apply?" has to be
answerable by looking at the output rather than by reasoning about discovery rules.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rayspec.cli.app import app

WF = """
rayspec: 1
name: quiet
isolation: none
agents:
  triage: {provider: stub, access: read-only}
steps:
  - {id: a, agent: triage, prompt: hi}
"""

POLICY = "access:\n  max: read-only\n"


def _project(project: Path, *, policy: str | None = None) -> Path:
    (project / ".rayspec" / "workflows" / "quiet.yaml").write_text(WF, encoding="utf-8")
    if policy is not None:
        (project / ".rayspec" / "policy.yaml").write_text(policy, encoding="utf-8")
    return project


def test_validate_names_the_layers_in_force(cli: CliRunner, home: Path, project: Path) -> None:
    _project(project, policy=POLICY)
    res = cli.invoke(app, ["validate", "quiet", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "policy: .rayspec/policy.yaml" in res.output


def test_validate_says_so_when_no_policy_is_in_force(
    cli: CliRunner, home: Path, project: Path
) -> None:
    """The absent case is the one that matters: it names the paths that were searched."""
    _project(project)
    res = cli.invoke(app, ["validate", "quiet", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "policy: none in force" in res.output
    assert str(project / ".rayspec" / "policy.yaml") in res.output


def test_validate_json_carries_the_layers(cli: CliRunner, home: Path, project: Path) -> None:
    _project(project, policy=POLICY)
    res = cli.invoke(app, ["validate", "quiet", "--root", str(project), "--json"])
    assert res.exit_code == 0, res.output
    (row,) = json.loads(res.stdout)
    assert row["policy"]["layers"] == [".rayspec/policy.yaml"]
    assert str(project / ".rayspec" / "policy.yaml") in row["policy"]["searched"]


def test_a_policy_outside_the_root_is_visibly_not_read(
    cli: CliRunner, home: Path, project: Path, tmp_path: Path
) -> None:
    """The ``--root`` trap: the workflow's own project has a policy, the root passed does not."""
    _project(project, policy=POLICY)
    empty = tmp_path / "empty"
    (empty / ".rayspec").mkdir(parents=True)
    res = cli.invoke(
        app,
        [
            "validate",
            str(project / ".rayspec" / "workflows" / "quiet.yaml"),
            "--root",
            str(empty),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "policy: none in force" in res.output
    assert str(empty / ".rayspec" / "policy.yaml") in res.output


def test_plan_names_the_layers_in_force(cli: CliRunner, home: Path, project: Path) -> None:
    _project(project, policy=POLICY)
    res = cli.invoke(app, ["plan", "quiet", "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert "policy: .rayspec/policy.yaml" in res.output


def test_plan_json_carries_the_layers(cli: CliRunner, home: Path, project: Path) -> None:
    _project(project, policy=POLICY)
    res = cli.invoke(app, ["plan", "quiet", "--root", str(project), "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)
    assert payload["policy"]["layers"] == [".rayspec/policy.yaml"]


def test_run_names_the_layers_in_force_on_stderr(cli: CliRunner, home: Path, project: Path) -> None:
    _project(project, policy=POLICY)
    res = cli.invoke(app, ["run", "quiet", "--root", str(project), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "policy: .rayspec/policy.yaml" in res.stderr
    assert "policy:" not in res.stdout
