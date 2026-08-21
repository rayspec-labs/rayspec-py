"""CLI-side review fixes: refusals, error routing, summaries and listings."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli import _runs_common as common
from rayspec.cli.app import app
from rayspec.providers.base import Usage
from rayspec.schema import RunStatus
from rayspec.store.file import FileRunStore

from .conftest import FAILED_ID, PAUSED_ID, SUCCEEDED_ID, T0, Seeded

SIMPLE = """
rayspec: 1
name: simple
isolation: none
inputs:
  topic: {type: string, required: true}
  count: {type: integer, default: 2}
steps:
  - {id: a, shell: "echo {{ inputs.topic }}"}
outputs:
  a: "{{ steps.a.output }}"
"""

GATE = """
rayspec: 1
name: gate
isolation: none
steps:
  - {id: a, shell: echo a}
  - {id: ok, needs: [a], approve: "ship?"}
  - {id: b, needs: [ok], shell: echo b}
"""

FAIL = """
rayspec: 1
name: fail
isolation: none
steps:
  - {id: boom, shell: "echo about to fail >&2; exit 3"}
"""


@pytest.fixture
def wf_project(home: Path, project: Path) -> Path:
    wfs = project / ".rayspec" / "workflows"
    (wfs / "simple.yaml").write_text(SIMPLE, encoding="utf-8")
    (wfs / "gate.yaml").write_text(GATE, encoding="utf-8")
    (wfs / "fail.yaml").write_text(FAIL, encoding="utf-8")
    return project


def _store(home: Path) -> FileRunStore:
    (slug_dir,) = [p for p in (home / "projects").glob("*/*") if (p / "runs").is_dir()]
    return FileRunStore(slug_dir)


# --------------------------------------------------------------------------------------------------
# run / resume refusals
# --------------------------------------------------------------------------------------------------


def test_run_resume_rejects_repo(cli: CliRunner, wf_project: Path) -> None:
    result = cli.invoke(
        app, ["run", "simple", "--resume", "2026", "--repo", "/x", "--root", str(wf_project)]
    )
    assert result.exit_code == 2, result.output
    assert "--repo cannot be combined with --resume" in result.output
    assert "rayspec resume" in result.output


def test_run_resume_refuses_a_run_of_another_workflow(
    cli: CliRunner, wf_project: Path, home: Path
) -> None:
    failed = cli.invoke(app, ["run", "fail", "--root", str(wf_project)])
    assert failed.exit_code == 1, failed.output
    (run_id,) = _store(home).list_run_ids()
    result = cli.invoke(
        app, ["run", "gate", "--resume", run_id, "--force", "--root", str(wf_project)]
    )
    assert result.exit_code == 2, result.output
    assert "belongs to workflow 'fail', not 'gate'" in result.output
    assert _store(home).load(run_id).workflow_name == "fail"


def test_resume_refuses_succeeded_and_cancelled_runs(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["resume", SUCCEEDED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 2, result.output
    assert "already succeeded" in result.output and "rayspec run fixit" in result.output
    run = seeded.store.load(PAUSED_ID)
    run.status = RunStatus.CANCELLED
    seeded.store.save(run)
    result = cli.invoke(app, ["resume", PAUSED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 2, result.output
    assert "was cancelled" in result.output


# --------------------------------------------------------------------------------------------------
# error routing: input/validation errors go to stderr as `error:` lines
# --------------------------------------------------------------------------------------------------


def test_input_errors_go_to_stderr_with_error_prefix(cli: CliRunner, wf_project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["run", "simple", "--root", str(wf_project)])
    assert result.exit_code == 2
    assert "error: missing required input(s): topic" in result.stderr
    assert "topic" not in result.stdout
    plan = runner.invoke(app, ["plan", "simple", "--root", str(wf_project)])
    assert plan.exit_code == 2
    assert "error: missing required input(s): topic" in plan.stderr
    as_json = runner.invoke(app, ["run", "simple", "--json", "--root", str(wf_project)])
    assert as_json.exit_code == 2
    payload = json.loads(as_json.stdout.strip().splitlines()[-1])
    assert payload["error"] == "input errors" and any("topic" in e for e in payload["errors"])


def test_validation_errors_go_to_stderr(cli: CliRunner, wf_project: Path) -> None:
    (wf_project / ".rayspec" / "workflows" / "tmpl.yaml").write_text(
        "rayspec: 1\nname: tmpl\nsteps:\n  - {id: a, shell: 'echo {{ inputs.missing }}'}\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["run", "tmpl", "--root", str(wf_project)])
    assert result.exit_code == 2
    assert "error: steps.a.shell" in result.stderr
    assert "inputs.missing" not in result.stdout


def test_missing_inputs_file_is_one_readable_error(cli: CliRunner, wf_project: Path) -> None:
    result = CliRunner().invoke(
        app, ["run", "simple", "--inputs-file", "missing.yaml", "--root", str(wf_project)]
    )
    assert result.exit_code == 2
    assert "inputs file 'missing.yaml' not found" in result.stderr
    assert "Errno" not in result.stderr
    assert "missing required" not in result.stderr


def test_root_must_be_a_directory(cli: CliRunner, home: Path) -> None:
    result = cli.invoke(app, ["workflows", "--root", "/nonexistent/dir"])
    assert result.exit_code == 2, result.output
    assert "--root '/nonexistent/dir' is not a directory" in result.output


def test_empty_workflows_prints_a_hint(cli: CliRunner, home: Path, project: Path) -> None:
    result = cli.invoke(app, ["workflows", "--root", str(project)])
    assert result.exit_code == 0
    assert "hint:" in result.output and ".rayspec/workflows/<name>.yaml" in result.output


def test_workflows_parse_error_is_short(cli: CliRunner, home: Path, project: Path) -> None:
    (project / ".rayspec" / "workflows" / "badyaml.yaml").write_text("a: [\n", encoding="utf-8")
    result = cli.invoke(app, ["workflows", "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert "parse error" in result.output and "rayspec validate" in result.output
    assert str(project) not in result.output.split("badyaml")[1].split("\n")[0]


def test_validate_unknown_workflow_is_an_error(cli: CliRunner, home: Path, project: Path) -> None:
    result = cli.invoke(app, ["validate", "bogus", "--root", str(project)])
    assert result.exit_code == 2
    assert "error: unknown workflow 'bogus'" in result.output
    assert "validated" not in result.output


def test_validate_and_plan_json(cli: CliRunner, wf_project: Path) -> None:
    result = cli.invoke(app, ["validate", "simple", "--json", "--root", str(wf_project)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data[0]["name"] == "simple" and data[0]["ok"] is True and data[0]["errors"] == []
    plan = cli.invoke(app, ["plan", "simple", "-i", "topic=x", "--json", "--root", str(wf_project)])
    assert plan.exit_code == 0, plan.output
    payload = json.loads(plan.output)
    assert payload["workflow"] == "simple"
    assert payload["inputs"]["topic"]["value"] == "x"
    assert payload["inputs"]["count"]["value"] == 2
    assert [s["path"] for s in payload["steps"]] == ["a"]


def test_plan_shows_resolved_inputs_next_to_the_bad_one(cli: CliRunner, wf_project: Path) -> None:
    result = cli.invoke(
        app, ["plan", "simple", "-i", "topic=x", "-i", "count=abc", "--root", str(wf_project)]
    )
    assert result.exit_code == 2
    assert "topic = x" in result.output
    assert "count = 'abc' (invalid" in result.output
    assert "missing (required)" not in result.output


def test_version_flag(cli: CliRunner) -> None:
    from rayspec import __version__

    result = cli.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output
    assert cli.invoke(app, ["-V"]).output == result.output


def test_cancel_without_tty_needs_yes(cli: CliRunner, seeded: Seeded, monkeypatch) -> None:
    run = seeded.store.load(FAILED_ID)
    run.status = RunStatus.RUNNING
    run.pid = 4242
    run.host = __import__("socket").gethostname()
    seeded.store.save(run)
    monkeypatch.setattr(common, "pid_alive", lambda r: True)
    monkeypatch.setattr(common, "pid_is_rayspec_run", lambda r: True)  # check passes
    monkeypatch.setattr(common, "interrupt_pid", lambda pid: None)
    result = cli.invoke(app, ["cancel", FAILED_ID, "--root", str(seeded.project)])  # stdin closed
    assert result.exit_code == 2, result.output
    assert "pass --yes" in result.output


def test_stubs_init_refuses_to_overwrite_without_force(cli: CliRunner, wf_project: Path) -> None:
    target = wf_project / "stubs.yaml"
    first = cli.invoke(
        app,
        ["run", "simple", "-i", "topic=x", "--stubs-init", str(target), "--root", str(wf_project)],
    )
    assert first.exit_code == 0, first.output
    second = cli.invoke(
        app,
        ["run", "simple", "-i", "topic=x", "--stubs-init", str(target), "--root", str(wf_project)],
    )
    assert second.exit_code == 2, second.output
    assert "already exists" in second.output and "--force" in second.output
    forced = cli.invoke(
        app,
        [
            "run",
            "simple",
            "-i",
            "topic=x",
            "--stubs-init",
            str(target),
            "--force",
            "--root",
            str(wf_project),
        ],
    )
    assert forced.exit_code == 0, forced.output


# --------------------------------------------------------------------------------------------------
# summaries
# --------------------------------------------------------------------------------------------------


def test_summary_is_printed_once_and_footer_is_labelled(cli: CliRunner, wf_project: Path) -> None:
    result = cli.invoke(app, ["run", "simple", "-i", "topic=x", "--root", str(wf_project)])
    assert result.exit_code == 0, result.output
    (run_id,) = [ln.split()[2] for ln in result.output.splitlines() if ln.startswith("■ run ")]
    assert result.output.count(f"run {run_id} succeeded") == 1
    assert "run dir: " in result.output
    assert " tok" not in result.output  # shell-only run: no token footer
    assert "awaiting approval" not in result.output


def test_paused_summary_has_one_hint_and_failed_summary_points_to_logs(
    cli: CliRunner, wf_project: Path
) -> None:
    paused = cli.invoke(app, ["run", "gate", "--no-interactive", "--root", str(wf_project)])
    assert paused.exit_code == 3, paused.output
    assert paused.output.count("awaiting approval at ok") == 1
    assert "rayspec approve" in paused.output
    failed = cli.invoke(app, ["run", "fail", "--root", str(wf_project)])
    assert failed.exit_code == 1, failed.output
    assert "exit: exit code" not in failed.output
    assert "exit code 3" in failed.output
    assert "rayspec logs" in failed.output and "rayspec resume" in failed.output


def test_resumed_summary_does_not_report_a_gate(
    cli: CliRunner, wf_project: Path, home: Path
) -> None:
    paused = cli.invoke(app, ["run", "gate", "--no-interactive", "--root", str(wf_project)])
    assert paused.exit_code == 3, paused.output
    (run_id,) = _store(home).list_run_ids()
    resumed = cli.invoke(
        app, ["run", "gate", "--resume", run_id, "--yes", "--root", str(wf_project)]
    )
    assert resumed.exit_code == 0, resumed.output
    assert "awaiting approval" not in resumed.output
    assert "↺ a reused" in resumed.output
    assert _store(home).load(run_id).pause is None


def test_dry_run_is_marked_in_runs_and_show(cli: CliRunner, wf_project: Path, home: Path) -> None:
    result = cli.invoke(
        app, ["run", "simple", "-i", "topic=x", "--dry-run", "--root", str(wf_project)]
    )
    assert result.exit_code == 0, result.output
    assert "not a git repository" not in result.output
    (run_id,) = _store(home).list_run_ids()
    assert _store(home).load(run_id).dry_run is True
    runs = cli.invoke(app, ["runs", "--root", str(wf_project)])
    assert "succeeded (dry)" in runs.output
    show = cli.invoke(app, ["show", run_id, "--root", str(wf_project)])
    assert "dry run" in show.output
    rows = json.loads(cli.invoke(app, ["runs", "--json", "--root", str(wf_project)]).output)
    assert rows[0]["dry_run"] is True


# --------------------------------------------------------------------------------------------------
# listings
# --------------------------------------------------------------------------------------------------


def test_fmt_cost_never_shows_tokens_as_cost() -> None:
    assert common.fmt_cost(None, "none", Usage(input=100, output=50)) == "-"
    assert common.fmt_cost(0.25, "provider", Usage(input=1)) == "$0.25"


def test_runs_has_a_tokens_column_and_sorts_by_created_at(cli: CliRunner, seeded: Seeded) -> None:
    # the failed run was created later than the paused one although its id sorts lower
    run = seeded.store.load(FAILED_ID)
    run.created_at = T0 + timedelta(hours=5)
    run.started_at = run.created_at
    seeded.store.save(run)
    result = cli.invoke(app, ["runs", "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if "2026" in line]
    assert [line.split()[0] for line in lines] == [FAILED_ID, PAUSED_ID, SUCCEEDED_ID]
    header = next(line for line in result.output.splitlines() if "tokens" in line)
    assert "cost" in header
    succeeded_line = lines[2]
    assert "8.5k" in succeeded_line and "$0.26" in succeeded_line


def test_show_paused_run_marks_the_pid_as_exited(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["show", PAUSED_ID, "--root", str(seeded.project)])
    assert result.exit_code == 0, result.output
    assert "pid:        4242 on nowhere (exited)" in result.output
    assert "cost: -" in result.output
