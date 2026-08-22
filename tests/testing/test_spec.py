"""The declarative case format: strict keys, did-you-mean errors with ``file:line``, discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.testing.spec import (
    Case,
    CaseFileError,
    Expect,
    StepExpect,
    Suite,
    discover_suites,
    load_cases,
    load_checks,
    unreachable_expect,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path: Path, text: str, name: str = "checks.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# -- shape ------------------------------------------------------------------------------------


def test_the_shipped_shape_still_parses(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
checks:
  - id: happy
    workflow: fix_issue
    inputs: { issue: 42 }
    stubs: stubs.yaml
    env: { A: '1', B: null }
    allow_unsupported: true
    validate: ok
    run: true
    expect:
      status: succeeded
      exit_code: 0
      outputs: { verdict: fix }
      steps: { bail: skipped }
      reason_contains: "nope"
""",
    )
    (case,) = load_checks(path)
    assert case.id == "happy"
    assert case.workflow == "fix_issue"
    assert case.inputs == {"issue": 42}
    assert case.stubs == (tmp_path / "stubs.yaml")  # resolved against the case file
    assert case.env == {"A": "1", "B": None}
    assert case.allow_unsupported is True
    assert case.validate_ == "ok"
    assert case.run is True
    assert case.expect.steps["bail"] == StepExpect(status="skipped")
    assert case.expect.reason_contains == "nope"


def test_default_id_is_workflow_and_index(tmp_path: Path) -> None:
    path = write(tmp_path, "checks:\n  - workflow: wf\n  - workflow: wf\n")
    assert [c.id for c in load_checks(path)] == ["wf-1", "wf-2"]


def test_per_step_expectations(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
checks:
  - workflow: wf
    expect:
      steps:
        review: succeeded
        bail:
          status: skipped
          skip_reason: "when: false"
        judge:
          output_regex: "LGTM"
          output_json: { verdict: ok }
""",
    )
    (case,) = load_checks(path)
    assert case.expect.steps["review"].status == "succeeded"
    assert case.expect.steps["bail"].skip_reason == "when: false"
    assert case.expect.steps["judge"].output_regex == "LGTM"
    assert case.expect.steps["judge"].output_json == {"verdict": "ok"}
    assert "output_json" in case.expect.steps["judge"].model_fields_set
    assert "output_json" not in case.expect.steps["review"].model_fields_set


# -- errors -----------------------------------------------------------------------------------


def test_unknown_expect_key_names_file_line_and_suggests(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "checks:\n  - workflow: wf\n    expect:\n      statuss: ok\n",
    )
    with pytest.raises(CaseFileError) as excinfo:
        load_checks(path)
    message = str(excinfo.value)
    assert "checks.yaml:4" in message, message
    assert "statuss" in message and "did you mean 'status'" in message, message


def test_unknown_case_key_names_the_line(tmp_path: Path) -> None:
    path = write(tmp_path, "checks:\n  - workflow: wf\n    stub: x.yaml\n")
    with pytest.raises(CaseFileError) as excinfo:
        load_checks(path)
    assert "checks.yaml:3" in str(excinfo.value)
    assert "did you mean 'stubs'" in str(excinfo.value)


def test_missing_workflow_is_reported_with_a_line(tmp_path: Path) -> None:
    path = write(tmp_path, "checks:\n  - inputs: {a: 1}\n")
    with pytest.raises(CaseFileError) as excinfo:
        load_checks(path)
    assert "workflow" in str(excinfo.value)
    assert "checks.yaml:2" in str(excinfo.value)


def test_every_problem_of_a_case_file_is_reported_together(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "checks:\n  - workflow: a\n    bogus: 1\n  - workflow: b\n    validate: maybe\n",
    )
    with pytest.raises(CaseFileError) as excinfo:
        load_checks(path)
    errors = excinfo.value.errors
    assert len(errors) == 2, errors
    assert "bogus" in errors[0] and errors[0].endswith("unknown field 'bogus' for case")
    assert "checks.yaml:3" in errors[0] and "checks.yaml:5" in errors[1]
    assert "validate: Input should be" in errors[1]


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    path = write(tmp_path, "checks:\n  - id: x\n    workflow: a\n  - id: x\n    workflow: b\n")
    with pytest.raises(CaseFileError, match="duplicate"):
        load_checks(path)


def test_not_a_mapping_is_a_case_file_error(tmp_path: Path) -> None:
    with pytest.raises(CaseFileError, match="checks"):
        load_checks(write(tmp_path, "- workflow: a\n"))


def test_invalid_yaml_is_a_case_file_error(tmp_path: Path) -> None:
    with pytest.raises(CaseFileError):
        load_checks(write(tmp_path, "checks: [\n"))


# -- greenfield layout ------------------------------------------------------------------------


def test_single_case_document(tmp_path: Path) -> None:
    path = write(tmp_path, "workflow: review\ninputs: {target: src/}\n", name="happy.yaml")
    cases, locations = load_cases(path, default_id="happy")
    assert [c.id for c in cases] == ["happy"]
    assert locations["happy"].of("workflow").endswith("happy.yaml:1")


def test_discovery_finds_suites_and_greenfield_cases(tmp_path: Path) -> None:
    (tmp_path / "examples" / "demo").mkdir(parents=True)
    (tmp_path / "examples" / "demo" / "checks.yaml").write_text(
        "checks:\n  - workflow: demo\n", encoding="utf-8"
    )
    dryrun = tmp_path / ".rayspec" / "dryrun"
    dryrun.mkdir(parents=True)
    (dryrun / "checks.yaml").write_text("checks:\n  - workflow: dog\n", encoding="utf-8")
    tests_dir = tmp_path / ".rayspec" / "tests" / "build"
    tests_dir.mkdir(parents=True)
    (tests_dir / "happy.yaml").write_text("workflow: build\n", encoding="utf-8")
    suites = discover_suites(tmp_path)
    by_name = {s.name: s for s in suites}
    assert set(by_name) == {"demo", "dogfood", "tests/build"}
    assert by_name["demo"].root == tmp_path / "examples" / "demo"
    assert by_name["dogfood"].root == tmp_path
    greenfield = by_name["tests/build"]
    assert greenfield.root == tmp_path
    assert [c.id for c in greenfield.checks] == ["happy"]
    assert greenfield.checks[0].workflow == "build"


def test_a_projects_own_checks_yaml_is_discovered(tmp_path: Path) -> None:
    """The layout a project scaffolded from an example has: the example IS the project, so its
    ``checks.yaml`` sits at the root — there is no ``examples/`` directory to put it under."""
    (tmp_path / "checks.yaml").write_text("checks:\n  - workflow: demo\n", encoding="utf-8")
    (suite,) = discover_suites(tmp_path)
    assert suite.name == "checks"
    assert suite.root == tmp_path
    assert suite.checks_path == tmp_path / "checks.yaml"
    assert [c.workflow for c in suite.checks] == ["demo"]


def test_a_root_checks_yaml_does_not_collide_with_the_example_suites(tmp_path: Path) -> None:
    (tmp_path / "checks.yaml").write_text("checks:\n  - workflow: own\n", encoding="utf-8")
    (tmp_path / "examples" / "demo").mkdir(parents=True)
    (tmp_path / "examples" / "demo" / "checks.yaml").write_text(
        "checks:\n  - workflow: demo\n", encoding="utf-8"
    )
    assert {s.name for s in discover_suites(tmp_path)} == {"checks", "demo"}


def test_a_root_document_that_is_not_a_case_file_is_left_alone(tmp_path: Path) -> None:
    """``checks.yaml`` at the root of an unrelated project may be somebody else's file."""
    (tmp_path / "checks.yaml").write_text("steps:\n  review: {text: hi}\n", encoding="utf-8")
    assert discover_suites(tmp_path) == []


def test_greenfield_case_workflow_defaults_to_the_directory(tmp_path: Path) -> None:
    tests_dir = tmp_path / ".rayspec" / "tests" / "release"
    tests_dir.mkdir(parents=True)
    (tests_dir / "dry.yaml").write_text("inputs: {tag: v1}\n", encoding="utf-8")
    (suite,) = discover_suites(tmp_path)
    assert suite.checks[0].workflow == "release"


def test_the_repo_suites_are_discovered() -> None:
    names = {s.name for s in discover_suites(REPO_ROOT)}
    assert {"hello_review", "fix_issue", "dogfood"} <= names


def test_suite_is_constructible_positionally(tmp_path: Path) -> None:
    """``scripts/check_examples.py`` and ``tests/examples`` build suites by hand."""
    suite = Suite("ex", tmp_path, tmp_path / "checks.yaml", ())
    assert suite.name == "ex" and suite.checks == ()
    assert suite.location("nope").of("status").endswith("checks.yaml")


def test_case_can_be_built_in_python(tmp_path: Path) -> None:
    case = Case(
        id="x", workflow="wf", stubs=tmp_path / "nope.yaml", expect=Expect(status="succeeded")
    )
    assert case.stubs == tmp_path / "nope.yaml"
    assert case.expect.status == "succeeded"
    assert case.run is True


# -- an `expect:` that is never evaluated ------------------------------------------------------


def test_run_false_with_expectations_is_refused(tmp_path: Path) -> None:
    """`run: false` never reaches the engine, so an `expect:` next to it is dead assertion."""
    path = write(
        tmp_path,
        "checks:\n  - workflow: wf\n    run: false\n    expect:\n      status: succeeded\n",
    )
    with pytest.raises(CaseFileError) as excinfo:
        load_checks(path)
    (error,) = excinfo.value.errors
    assert "checks.yaml:4" in error, error
    assert "never evaluated" in error, error
    assert excinfo.value.hint and "drop `run: false`" in excinfo.value.hint


def test_validate_error_with_expectations_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "checks:\n  - workflow: wf\n    validate: error\n    expect:\n      exit_code: 2\n",
    )
    with pytest.raises(CaseFileError, match="never evaluated"):
        load_checks(path)


def test_run_false_without_expectations_is_fine(tmp_path: Path) -> None:
    path = write(tmp_path, "checks:\n  - workflow: wf\n    run: false\n    expect: {}\n")
    (case,) = load_checks(path)
    assert case.run is False


def test_unreachable_expect_names_the_reason(tmp_path: Path) -> None:
    case = Case(workflow="wf", run=False, expect=Expect(status="succeeded"))
    assert unreachable_expect(case) == "run: false"
    assert unreachable_expect(Case(workflow="wf", run=False)) is None
    assert unreachable_expect(Case(workflow="wf", expect=Expect(status="ok"))) is None


# -- discovery is discriminating --------------------------------------------------------------


def test_a_stub_script_next_to_a_case_is_not_a_case(tmp_path: Path) -> None:
    """docs/testing.md tells users to put `stubs:` next to the case file; that must work."""
    tests_dir = tmp_path / ".rayspec" / "tests" / "demo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "happy.yaml").write_text("stubs: stubs.yaml\n", encoding="utf-8")
    (tests_dir / "stubs.yaml").write_text(
        "steps:\n  review: { text: LGTM }\ndefaults: { latency_ms: 0 }\n", encoding="utf-8"
    )
    (suite,) = discover_suites(tmp_path)
    assert [c.id for c in suite.checks] == ["happy"]


def test_a_stub_script_loose_in_the_tests_dir_is_not_a_case(tmp_path: Path) -> None:
    tests_dir = tmp_path / ".rayspec" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "solo.yaml").write_text("workflow: wf\n", encoding="utf-8")
    (tests_dir / "stubs.yaml").write_text("steps:\n  review: { text: LGTM }\n", encoding="utf-8")
    (suite,) = discover_suites(tmp_path)
    assert [c.id for c in suite.checks] == ["solo"]


def test_a_typo_in_a_case_file_is_still_refused(tmp_path: Path) -> None:
    """Skipping is only for a document that is recognisably NOT a case."""
    tests_dir = tmp_path / ".rayspec" / "tests" / "demo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "typo.yaml").write_text("workfloww: wf\ninputs: {a: 1}\n", encoding="utf-8")
    with pytest.raises(CaseFileError, match="workfloww"):
        discover_suites(tmp_path)


def test_duplicate_ids_across_greenfield_case_files_are_refused(tmp_path: Path) -> None:
    tests_dir = tmp_path / ".rayspec" / "tests" / "demo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "one.yaml").write_text("id: same\nworkflow: wf\n", encoding="utf-8")
    (tests_dir / "two.yaml").write_text("id: same\nworkflow: wf\n", encoding="utf-8")
    with pytest.raises(CaseFileError) as excinfo:
        discover_suites(tmp_path)
    (error,) = excinfo.value.errors
    assert "duplicate case id 'same'" in error, error
    assert "one.yaml" in error and "two.yaml" in error, error


def test_duplicate_ids_across_loose_case_files_are_refused(tmp_path: Path) -> None:
    tests_dir = tmp_path / ".rayspec" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "a.yaml").write_text("id: same\nworkflow: wf\n", encoding="utf-8")
    (tests_dir / "b.yaml").write_text("id: same\nworkflow: wf\n", encoding="utf-8")
    with pytest.raises(CaseFileError, match="duplicate case id 'same'"):
        discover_suites(tmp_path)


def test_the_fallback_location_of_a_directory_suite_is_repo_relative(tmp_path: Path) -> None:
    tests_dir = tmp_path / ".rayspec" / "tests" / "demo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "happy.yaml").write_text("workflow: wf\n", encoding="utf-8")
    (suite,) = discover_suites(tmp_path)
    assert suite.location("nope").of() == ".rayspec/tests/demo"


def test_a_single_typo_key_is_not_mistaken_for_a_stub_script(tmp_path: Path) -> None:
    """Only a document that positively looks like a stub script is skipped."""
    tests_dir = tmp_path / ".rayspec" / "tests" / "demo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "typo.yaml").write_text("workflowz: wf\n", encoding="utf-8")
    with pytest.raises(CaseFileError, match="workflowz"):
        discover_suites(tmp_path)


def test_an_empty_case_file_is_still_a_case(tmp_path: Path) -> None:
    tests_dir = tmp_path / ".rayspec" / "tests" / "demo"
    tests_dir.mkdir(parents=True)
    (tests_dir / "bare.yaml").write_text("", encoding="utf-8")
    (suite,) = discover_suites(tmp_path)
    assert [c.id for c in suite.checks] == ["bare"]
