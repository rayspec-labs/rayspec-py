# SPDX-License-Identifier: Apache-2.0
"""``rayspec runs diff <a> <b>`` — what moved between two runs of one workflow.

The comparison is deliberately narrow: two runs of the SAME workflow. Comparing runs of two
different workflows is refused (naming both) rather than guessed at, and ``--exit-code`` turns
the command into a CI gate.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.schema import StepStatus
from rayspec.store.model import StepRecord

from .conftest import FAILED_ID, OTHER_SLUG, SUCCEEDED_ID, Seeded

WF = """
rayspec: 1
name: mixer
isolation: none
agents:
  bot: {provider: stub}
steps:
  - id: build
    loop:
      max_iterations: 2
      on_exhausted: continue
      steps:
        - id: implement
          agent: bot
          prompt: "iteration {{ iteration.n }}"
outputs:
  last: "{{ steps.build.output.implement }}"
"""

STUBS_A = 'steps:\n  "build[*]/implement": {sequence: ["alpha one", "alpha two"]}\n'
STUBS_B = 'steps:\n  "build[*]/implement": {sequence: ["beta one", "beta two"]}\n'


def _write(project: Path, name: str, text: str) -> None:
    (project / ".rayspec" / "workflows" / f"{name}.yaml").write_text(text, encoding="utf-8")


def _run(cli: CliRunner, project: Path, stubs_text: str) -> str:
    stubs = project / "s.yaml"
    stubs.write_text(stubs_text, encoding="utf-8")
    result = cli.invoke(
        app,
        ["run", "mixer", "--root", str(project), "--dry-run", "--json", "--stubs", str(stubs)],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout.strip().splitlines()[-1])["run_id"]


def _two_runs(cli: CliRunner, project: Path) -> tuple[str, str]:
    _write(project, "mixer", WF)
    return _run(cli, project, STUBS_A), _run(cli, project, STUBS_B)


# -- refusals ---------------------------------------------------------------------------------


def test_diff_across_two_workflows_exits_2_naming_both(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(
        app, ["runs", "diff", SUCCEEDED_ID, FAILED_ID, "--root", str(seeded.project)]
    )
    assert result.exit_code == 2
    assert "fixit" in result.output and "deploy" in result.output


def test_diff_of_a_missing_run_exits_2(cli: CliRunner, seeded: Seeded) -> None:
    result = cli.invoke(app, ["runs", "diff", SUCCEEDED_ID, "nope", "--root", str(seeded.project)])
    assert result.exit_code == 2


# -- deltas -----------------------------------------------------------------------------------


def test_diff_reports_the_real_deltas(cli: CliRunner, home: Path, project: Path) -> None:
    a, b = _two_runs(cli, project)
    result = cli.invoke(app, ["runs", "diff", a, b, "--root", str(project)])
    assert result.exit_code == 0, result.output
    assert a in result.output and b in result.output
    assert "mixer" in result.output
    # both runs succeeded, but every prompt answer moved
    assert "build[1]/implement" in result.output
    assert "alpha two" in result.output or "last" in result.output


def test_diff_json_shape(cli: CliRunner, home: Path, project: Path) -> None:
    a, b = _two_runs(cli, project)
    result = cli.invoke(app, ["runs", "diff", a, b, "--json", "--root", str(project)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["workflow"] == "mixer"
    assert payload["a"]["run_id"] == a and payload["b"]["run_id"] == b
    assert payload["changed"] is True
    assert payload["status"]["changed"] is False  # both succeeded
    steps = {row["path"]: row for row in payload["steps"]}
    assert steps["build[1]/implement"]["change"] == "changed"
    assert steps["build[1]/implement"]["output"]["changed"] is True
    assert steps["build[1]/implement"]["status"]["a"] == "succeeded"
    assert payload["outputs"]["last"]["a"] == "alpha two"
    assert payload["outputs"]["last"]["b"] == "beta two"
    assert payload["outputs"]["last"]["changed"] is True
    assert payload["loops"][0]["path"] == "build"
    assert payload["loops"][0]["a"]["iterations"] == 2
    assert "tokens" in payload and "duration_ms" in payload and "cost_usd" in payload


def test_exit_code_flag_is_a_ci_gate(cli: CliRunner, home: Path, project: Path) -> None:
    a, b = _two_runs(cli, project)
    differ = cli.invoke(app, ["runs", "diff", a, b, "--exit-code", "--root", str(project)])
    assert differ.exit_code == 1
    same = cli.invoke(app, ["runs", "diff", a, a, "--exit-code", "--root", str(project)])
    assert same.exit_code == 0, same.output
    assert "no differences" in same.output


def test_outputs_flag_shows_a_unified_step_output_diff(
    cli: CliRunner, home: Path, project: Path
) -> None:
    a, b = _two_runs(cli, project)
    plain = cli.invoke(app, ["runs", "diff", a, b, "--root", str(project)])
    assert "-alpha one" not in plain.output
    detailed = cli.invoke(app, ["runs", "diff", a, b, "--outputs", "--root", str(project)])
    assert detailed.exit_code == 0, detailed.output
    assert "-alpha one" in detailed.output and "+beta one" in detailed.output


def test_steps_flag_lists_unchanged_steps_too(cli: CliRunner, home: Path, project: Path) -> None:
    _write(project, "mixer", WF)
    a = _run(cli, project, STUBS_A)
    b = _run(cli, project, STUBS_A)
    plain = cli.invoke(app, ["runs", "diff", a, b, "--root", str(project)])
    assert plain.exit_code == 0, plain.output
    assert "build[1]/implement" not in plain.output
    listed = cli.invoke(app, ["runs", "diff", a, b, "--steps", "--root", str(project)])
    assert "build[1]/implement" in listed.output


def test_identical_runs_are_not_a_difference(cli: CliRunner, home: Path, project: Path) -> None:
    _write(project, "mixer", WF)
    a = _run(cli, project, STUBS_A)
    b = _run(cli, project, STUBS_A)
    payload = json.loads(
        cli.invoke(app, ["runs", "diff", a, b, "--json", "--root", str(project)]).stdout
    )
    assert payload["changed"] is False
    assert all(row["change"] == "same" for row in payload["steps"])


def test_added_and_removed_steps_are_reported(cli: CliRunner, home: Path, project: Path) -> None:
    _write(project, "mixer", WF)
    a = _run(cli, project, STUBS_A)
    _write(project, "mixer", WF.replace("max_iterations: 2", "max_iterations: 3"))
    b = _run(cli, project, 'steps:\n  "build[*]/implement": {text: "same"}\n')
    payload = json.loads(
        cli.invoke(app, ["runs", "diff", a, b, "--json", "--root", str(project)]).stdout
    )
    steps = {row["path"]: row for row in payload["steps"]}
    assert steps["build[3]/implement"]["change"] == "added"
    assert payload["workflow_hash_changed"] is True
    assert payload["changed"] is True


# -- unrelated runs ---------------------------------------------------------------------------


def _clone_into_other_project(seeded: Seeded, run_id: str, new_id: str) -> str:
    """The same workflow name, a different project — what `lookup_run`'s home-wide fallback finds."""
    run = seeded.store.load(run_id)
    run.run_id = new_id
    run.project_slug = OTHER_SLUG
    run.project_root = str(seeded.project / "other")
    seeded.other_store.create(run)
    return new_id


def test_diff_across_two_projects_is_refused(cli: CliRunner, seeded: Seeded) -> None:
    """Same workflow NAME, two unrelated repos: a plausible-looking report about nothing."""
    other = _clone_into_other_project(seeded, SUCCEEDED_ID, "20260820-100500-cccc")
    result = cli.invoke(app, ["runs", "diff", SUCCEEDED_ID, other, "--root", str(seeded.project)])
    assert result.exit_code == 2, result.output
    assert seeded.slug in result.output and OTHER_SLUG in result.output
    assert "--across-projects" in result.output


def test_across_projects_names_both_projects_in_the_header(cli: CliRunner, seeded: Seeded) -> None:
    other = _clone_into_other_project(seeded, SUCCEEDED_ID, "20260820-100500-dddd")
    result = cli.invoke(
        app,
        ["runs", "diff", SUCCEEDED_ID, other, "--across-projects", "--root", str(seeded.project)],
    )
    assert result.exit_code == 0, result.output
    assert "project" in result.output
    assert seeded.slug in result.output and OTHER_SLUG in result.output


def test_outputs_of_a_run_with_secret_inputs_warns(cli: CliRunner, seeded: Seeded) -> None:
    """`runs stubs` refuses such a run outright; `--outputs` prints stored text, so it must at
    least say that the redactor is not there yet."""
    run = seeded.store.load(SUCCEEDED_ID)
    run.secret_inputs = ("token",)
    seeded.store.save(run)
    result = cli.invoke(
        app,
        ["runs", "diff", SUCCEEDED_ID, SUCCEEDED_ID, "--outputs", "--root", str(seeded.project)],
    )
    assert result.exit_code == 0, result.output
    assert "token" in result.output and "secret" in result.output


NASTY_PATH = "ask\x1b[31mred"


def test_step_paths_in_the_output_diff_are_control_safe(cli: CliRunner, seeded: Seeded) -> None:
    """Every other path render in `render` goes through `safe_text`; the `--- step` line did not."""
    for run_id, text in ((SUCCEEDED_ID, "one"), ("20260820-100700-ffff", "two")):
        if run_id != SUCCEEDED_ID:
            run = seeded.store.load(SUCCEEDED_ID)
            run.run_id = run_id
            seeded.store.create(run)
        run = seeded.store.load(run_id)
        ref = seeded.store.write_output(run_id, "ask", text, kind="text")
        run.steps[NASTY_PATH] = StepRecord(
            path=NASTY_PATH,
            id="red",
            kind="prompt",
            status=StepStatus.SUCCEEDED,
            output_ref=ref,
            output_kind="text",
            output_sha256=text,
        )
        seeded.store.save(run)
    result = cli.invoke(
        app,
        [
            "runs",
            "diff",
            SUCCEEDED_ID,
            "20260820-100700-ffff",
            "--outputs",
            "--root",
            str(seeded.project),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "\x1b[31m" not in result.output
    assert "askred" in result.output
