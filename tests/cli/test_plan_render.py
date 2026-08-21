# SPDX-License-Identifier: Apache-2.0
"""`rayspec plan --render [--step PATH] [--stubs FILE] [--json]` — the token-free preview.

"See exactly what the agent will receive before spending a token": prompt bodies and
shell/python scripts rendered with stubbed (or placeholder) upstream values, with every
``${RAYSPEC_V<n>}`` slot shown next to its value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.cli.commands.run import project_slug_for
from rayspec.store.file import FileRunStore

WF = """
rayspec: 1
name: t
isolation: none
inputs:
  topic: {type: string, default: bugs}
  token: {type: string, secret: true, required: false}
agents:
  reviewer: {provider: stub, model: small}
steps:
  - id: fetch
    shell: "printf fix"
  - id: assess
    needs: [fetch]
    agent: reviewer
    prompt: "Assess {{ steps.fetch.output }} about {{ inputs.topic }}"
  - id: patch
    needs: [assess]
    env: {TOKEN: "{{ inputs.token | default('none') }}"}
    shell: |
      echo "{{ steps.assess.output }}"
      echo "{{ inputs.topic }}"
"""

STUBS = """
steps:
  fetch: {text: "a real diff"}
  assess: {text: "verdict: fix"}
"""


@pytest.fixture
def project_with_wf(project: Path) -> Path:
    (project / ".rayspec" / "workflows" / "t.yaml").write_text(WF, encoding="utf-8")
    (project / ".rayspec" / "stubs.yaml").write_text(STUBS, encoding="utf-8")
    return project


def plan(cli: CliRunner, project: Path, *args: str):
    return cli.invoke(app, ["plan", "t", "--root", str(project), *args])


def test_render_shows_every_leaf_step_with_placeholder_upstream_values(
    cli: CliRunner, project_with_wf: Path
) -> None:
    result = plan(cli, project_with_wf, "--render")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "assess" in out and "patch" in out
    # no stubs: an upstream output is a visible placeholder, never an empty string
    assert "<fetch output>" in out
    assert "about bugs" in out


def test_stubs_feed_the_upstream_values(cli: CliRunner, project_with_wf: Path) -> None:
    stubs = str(project_with_wf / ".rayspec" / "stubs.yaml")
    result = plan(cli, project_with_wf, "--render", "--stubs", stubs)
    assert result.exit_code == 0, result.output
    assert "Assess a real diff about bugs" in result.output


def test_step_renders_only_that_step_with_its_env_slots(
    cli: CliRunner, project_with_wf: Path
) -> None:
    stubs = str(project_with_wf / ".rayspec" / "stubs.yaml")
    result = plan(cli, project_with_wf, "--render", "--step", "patch", "--stubs", stubs)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Assess" not in out  # only the requested step
    assert "${RAYSPEC_V1}" in out and "${RAYSPEC_V2}" in out
    assert "verdict: fix" in out and "bugs" in out
    assert "TOKEN" in out


def test_a_secret_input_is_never_rendered(cli: CliRunner, project_with_wf: Path) -> None:
    result = plan(cli, project_with_wf, "--render", "--step", "patch", "-i", "token=hunter2")
    assert result.exit_code == 0, result.output
    assert "hunter2" not in result.output
    assert "<secret>" in result.output


def test_render_json_payload(cli: CliRunner, project_with_wf: Path) -> None:
    stubs = str(project_with_wf / ".rayspec" / "stubs.yaml")
    result = plan(cli, project_with_wf, "--render", "--json", "--stubs", stubs)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    rendered = {entry["path"]: entry for entry in data["render"]}
    assert set(rendered) == {"fetch", "assess", "patch"}
    assert rendered["assess"]["text"] == "Assess a real diff about bugs"
    assert rendered["assess"]["kind"] == "prompt" and rendered["assess"]["agent"] == "reviewer"
    assert rendered["patch"]["env"]["RAYSPEC_V1"] == "verdict: fix"
    assert data["workflow"] == "t"  # the usual plan payload is still there
    assert data["stubs"].endswith("stubs.yaml")


def test_step_or_stubs_without_render_is_a_usage_error(
    cli: CliRunner, project_with_wf: Path
) -> None:
    assert plan(cli, project_with_wf, "--step", "patch").exit_code == 2
    stubs = str(project_with_wf / ".rayspec" / "stubs.yaml")
    assert plan(cli, project_with_wf, "--stubs", stubs).exit_code == 2


def test_unknown_step_and_unrenderable_step_are_usage_errors(
    cli: CliRunner, project_with_wf: Path
) -> None:
    unknown = plan(cli, project_with_wf, "--render", "--step", "nope")
    assert unknown.exit_code == 2 and "nope" in unknown.output


GLOB_FIRST_STUBS = """
steps:
  '*': {text: "FROM_GLOB"}
  assess: {text: "FROM_EXACT"}
"""


def test_the_preview_resolves_stubs_exactly_like_the_run(
    cli: CliRunner, project_with_wf: Path, home: Path
) -> None:
    """A preview that disagrees with the run it previews is worse than no preview."""
    stubs = project_with_wf / ".rayspec" / "glob-first.yaml"
    stubs.write_text(GLOB_FIRST_STUBS, encoding="utf-8")
    ran = cli.invoke(
        app,
        ["run", "t", "--root", str(project_with_wf), "--quiet", "--stubs", str(stubs)],
    )
    assert ran.exit_code == 0, ran.output
    store = FileRunStore(home / "projects" / project_slug_for(project_with_wf))
    run_id = store.list_run_ids()[0]
    assert store.read_output(run_id, "steps/assess/output.txt") == "FROM_EXACT"

    preview = plan(cli, project_with_wf, "--render", "--json", "--stubs", str(stubs))
    assert preview.exit_code == 0, preview.output
    rows = {row["def_path"]: row for row in json.loads(preview.output)["render"]}
    assert rows["patch"]["env"]["RAYSPEC_V1"] == "FROM_EXACT"


WARN_WF = """
rayspec: 1
name: w
isolation: none
defaults:
  on_unsupported: warn
agents:
  fixer:
    provider: codex
    model: medium
    max_turns: 40
    tools: {deny: [edit]}
steps:
  - id: fix
    agent: fixer
    prompt: "fix it"
"""


def test_render_prints_the_warnings_the_plain_plan_prints(
    cli: CliRunner, project_with_wf: Path
) -> None:
    """`--render` is still `plan`: hiding `on_unsupported: warn` findings from it is silent."""
    (project_with_wf / ".rayspec" / "workflows" / "w.yaml").write_text(WARN_WF, encoding="utf-8")

    def run(*args: str):
        return cli.invoke(app, ["plan", "w", "--root", str(project_with_wf), *args])

    expected = json.loads(run("--json").output)["warnings"]
    assert any("max_turns" in w for w in expected), expected
    plain, rendered = run(), run("--render")
    assert plain.exit_code == 0 and rendered.exit_code == 0, rendered.output
    assert json.loads(run("--render", "--json").output)["warnings"] == expected
    for warning in expected:
        assert warning.splitlines()[0] in plain.output
        assert warning.splitlines()[0] in rendered.output


LOOP_WF = """
rayspec: 1
name: lp
isolation: none
steps:
  - id: build
    loop:
      max_iterations: 3
      until: "iteration.n == 3"
      steps:
        - id: echo
          shell: "printf {{ iteration.n }}"
  - id: fan
    each: "['a', 'b']"
    as: letter
    steps:
      - id: work
        shell: "printf {{ letter }}"
"""


def test_render_rows_report_the_record_path_they_previewed(
    cli: CliRunner, project_with_wf: Path
) -> None:
    """`path` must be the indexed path the preview bound — stubs now key on it."""
    (project_with_wf / ".rayspec" / "workflows" / "lp.yaml").write_text(LOOP_WF, encoding="utf-8")
    result = cli.invoke(app, ["plan", "lp", "--root", str(project_with_wf), "--render", "--json"])
    assert result.exit_code == 0, result.output
    rows = {row["def_path"]: row for row in json.loads(result.output)["render"]}
    assert rows["build/echo"]["path"] == "build[1]/echo"
    assert rows["fan/work"]["path"] == "fan[0]/work"
