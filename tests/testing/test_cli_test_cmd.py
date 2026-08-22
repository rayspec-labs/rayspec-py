"""``rayspec test`` — discovery, filters, exit codes, ``--junit`` and ``--json``."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app

SUITE = """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect:
      status: succeeded
      outputs: { verdict: LGTM }
  - id: strict
    workflow: demo
    stubs: stubs.yaml
    expect:
      steps: { review: succeeded }
"""

GREENFIELD = """
stubs: ../../../stubs.yaml
expect:
  status: succeeded
"""


@pytest.fixture
def cases(project: Path) -> Path:
    """The demo project with a greenfield ``.rayspec/tests/demo/`` suite."""
    tests_dir = project / ".rayspec" / "tests" / "demo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "happy.yaml").write_text(GREENFIELD, encoding="utf-8")
    (tests_dir / "second.yaml").write_text(GREENFIELD, encoding="utf-8")
    return project


def invoke(args: list[str], project: Path, home: Path):
    runner = CliRunner(env={"RAYSPEC_HOME": str(home), "NO_COLOR": "1"})
    return runner.invoke(app, ["test", "--root", str(project), *args], catch_exceptions=False)


def test_green_run_exits_zero_and_names_every_case(cases: Path, home: Path) -> None:
    result = invoke([], cases, home)
    assert result.exit_code == 0, result.output
    assert "tests/demo:happy" in result.output
    assert "tests/demo:second" in result.output
    assert "2 passed" in result.output


def test_a_failing_expectation_exits_one_and_names_file_line(cases: Path, home: Path) -> None:
    path = cases / ".rayspec" / "tests" / "demo" / "happy.yaml"
    path.write_text(GREENFIELD.replace("succeeded", "failed"), encoding="utf-8")
    result = invoke([], cases, home)
    assert result.exit_code == 1, result.output
    assert "expect.status" in result.output
    assert ".rayspec/tests/demo/happy.yaml:4" in result.output, result.output
    assert "1 passed, 1 failed" in result.output


def test_filters(cases: Path, home: Path) -> None:
    assert invoke(["--case", "happy"], cases, home).output.count("tests/demo:") == 1
    assert "second" not in invoke(["-k", "happy"], cases, home).output
    assert invoke(["demo"], cases, home).exit_code == 0


def test_an_unknown_filter_is_a_usage_error(cases: Path, home: Path) -> None:
    result = invoke(["--case", "nope"], cases, home)
    assert result.exit_code == 2, result.output
    assert "nope" in result.output and "happy" in result.output


def test_an_unknown_workflow_argument_is_a_usage_error(cases: Path, home: Path) -> None:
    result = invoke(["nope"], cases, home)
    assert result.exit_code == 2, result.output


def test_no_cases_at_all_is_a_usage_error(project: Path, home: Path) -> None:
    result = invoke([], project, home)
    assert result.exit_code == 2, result.output
    assert ".rayspec/tests" in result.output


def test_a_malformed_case_file_is_a_usage_error(cases: Path, home: Path) -> None:
    path = cases / ".rayspec" / "tests" / "demo" / "happy.yaml"
    path.write_text("expect: {statuss: ok}\n", encoding="utf-8")
    result = invoke([], cases, home)
    assert result.exit_code == 2, result.output
    assert "statuss" in result.output
    assert "happy.yaml:1" in result.output


def test_junit_file_parses(cases: Path, home: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.xml"
    result = invoke(["--junit", str(out)], cases, home)
    assert result.exit_code == 0, result.output
    root = ET.parse(out).getroot()
    assert root.tag == "testsuites" and root.get("tests") == "2"
    assert {c.get("name") for s in root for c in s} == {"happy", "second"}


def test_junit_is_written_even_when_cases_fail(cases: Path, home: Path, tmp_path: Path) -> None:
    path = cases / ".rayspec" / "tests" / "demo" / "happy.yaml"
    path.write_text(GREENFIELD.replace("succeeded", "failed"), encoding="utf-8")
    out = tmp_path / "out.xml"
    assert invoke(["--junit", str(out)], cases, home).exit_code == 1
    root = ET.parse(out).getroot()
    assert root.get("failures") == "1"


def test_json_output_is_one_object_on_stdout(cases: Path, home: Path) -> None:
    result = invoke(["--json"], cases, home)
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["passed"] == 2 and payload["failed"] == 0
    assert {c["case"] for c in payload["cases"]} == {"happy", "second"}
    assert all(c["run_id"] for c in payload["cases"])


def test_suite_style_checks_yaml_next_to_a_project(project: Path, home: Path) -> None:
    """A ``checks.yaml`` under ``examples/<name>/`` is discovered as its own suite."""
    example = project / "examples" / "demo"
    example.mkdir(parents=True)
    (example / ".rayspec").mkdir()
    for child in (project / ".rayspec").iterdir():
        if child.is_dir():
            (example / ".rayspec" / child.name).mkdir(exist_ok=True)
            for f in child.iterdir():
                (example / ".rayspec" / child.name / f.name).write_bytes(f.read_bytes())
    (example / "stubs.yaml").write_bytes((project / "stubs.yaml").read_bytes())
    (example / "checks.yaml").write_text(SUITE, encoding="utf-8")
    result = invoke([], project, home)
    assert result.exit_code == 0, result.output
    assert "demo:happy" in result.output and "2 passed" in result.output


def test_exec_shell_flag_is_accepted(cases: Path, home: Path) -> None:
    assert invoke(["--exec-shell"], cases, home).exit_code == 0


# -- a data file may never widen what the command does -----------------------------------------

SIDE_EFFECT_WORKFLOW = """
rayspec: 1
name: probe
description: A workflow whose shell step has an observable side effect.
isolation: none
steps:
  - id: touch
    shell: "touch \\"$PROBE\\" && echo touched"
outputs:
  done: "{{ steps.touch.output | trim }}"
"""


@pytest.fixture
def side_effect_project(project: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A project with one case that asks for `exec_shell:` and a marker the step would create."""
    marker = tmp_path / "SIDE_EFFECT"
    (project / ".rayspec" / "workflows" / "probe.yaml").write_text(
        SIDE_EFFECT_WORKFLOW, encoding="utf-8"
    )
    tests_dir = project / ".rayspec" / "tests" / "probe"
    tests_dir.mkdir(parents=True)
    (tests_dir / "run.yaml").write_text(
        f"exec_shell: true\nenv: {{ PROBE: {marker} }}\nexpect: {{ status: succeeded }}\n",
        encoding="utf-8",
    )
    return project, marker


def test_a_case_asking_for_exec_shell_is_refused_without_the_flag(
    side_effect_project: tuple[Path, Path], home: Path
) -> None:
    """A committed `exec_shell: true` must not turn `rayspec test` into code execution."""
    project, marker = side_effect_project
    result = invoke([], project, home)
    assert result.exit_code == 2, result.output
    assert "exec_shell" in result.output and "--exec-shell" in result.output
    assert ".rayspec/tests/probe/run.yaml:1" in result.output, result.output
    assert not marker.exists(), "the shell step ran without --exec-shell"


def test_the_flag_authorises_the_case(side_effect_project: tuple[Path, Path], home: Path) -> None:
    project, marker = side_effect_project
    result = invoke(["--exec-shell"], project, home)
    assert result.exit_code == 0, result.output
    assert marker.exists()


def test_the_project_env_file_is_not_loaded(project: Path, home: Path) -> None:
    """Cases are dry runs against the stub provider — they need no credentials."""
    (project / ".rayspec" / ".env").write_text("HARNESS_ENV_PROBE=leaked\n", encoding="utf-8")
    tests_dir = project / ".rayspec" / "tests" / "demo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "happy.yaml").write_text(GREENFIELD, encoding="utf-8")
    import os

    os.environ.pop("HARNESS_ENV_PROBE", None)
    result = invoke([], project, home)
    assert result.exit_code == 0, result.output
    assert os.environ.get("HARNESS_ENV_PROBE") is None
    assert "loaded 1 variable from .rayspec/.env" not in result.output


def test_junit_is_written_on_a_usage_exit(cases: Path, home: Path, tmp_path: Path) -> None:
    """docs promise the file exists for exit 2 as well, so a CI publish step has something."""
    out = tmp_path / "results.xml"
    result = invoke(["-k", "does-not-exist", "--junit", str(out)], cases, home)
    assert result.exit_code == 2, result.output
    assert out.is_file(), "no JUnit file written for a usage exit"
    tree = ET.parse(out)
    assert tree.getroot().get("errors") == "1"
    assert "no test case matches" in ET.tostring(tree.getroot(), encoding="unicode")


def test_junit_is_written_when_a_case_file_is_malformed(
    cases: Path, home: Path, tmp_path: Path
) -> None:
    out = tmp_path / "results.xml"
    (cases / ".rayspec" / "tests" / "demo" / "happy.yaml").write_text(
        "workflowz: nope\n", encoding="utf-8"
    )
    result = invoke(["--junit", str(out)], cases, home)
    assert result.exit_code == 2, result.output
    assert out.is_file()


def test_the_no_cases_hint_offers_a_placement_that_works(tmp_path: Path) -> None:
    """A wheel user has no ``examples/`` directory: the hint may only name places `rayspec test`
    actually discovers from the project root it was given."""
    from rayspec.cli.commands.test import NO_CASES_HINT

    # "next to an example" is the repository's own layout: `examples/<name>/checks.yaml` is
    # discovered relative to the project root, which a scaffolded project has no `examples/` in
    assert "next to an example" not in NO_CASES_HINT
    assert "checks.yaml at the project root" in NO_CASES_HINT
    (tmp_path / ".rayspec" / "workflows").mkdir(parents=True)
    (tmp_path / ".rayspec" / "workflows" / "wf.yaml").write_text(
        'rayspec: 1\nname: wf\nsteps:\n  - {id: a, shell: "true"}\n', encoding="utf-8"
    )
    (tmp_path / "checks.yaml").write_text(
        "checks:\n  - {id: smoke, workflow: wf}\n", encoding="utf-8"
    )
    from rayspec.testing import discover_suites

    assert [s.name for s in discover_suites(tmp_path)] == ["checks"]
