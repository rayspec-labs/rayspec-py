"""``run_case`` executes a case through the engine and reports actionable failures."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from rayspec.engine.runner import fallback_project_slug
from rayspec.store.file import FileRunStore
from rayspec.testing.report import results_json
from rayspec.testing.runner import run_case
from rayspec.testing.spec import Expect, Suite, load_cases


def suite_of(project: Path, text: str, name: str = "demo") -> Suite:
    """Write ``checks.yaml`` into the project and load it as a suite."""
    path = project / "checks.yaml"
    path.write_text(text, encoding="utf-8")
    cases, locations = load_cases(path, root=project)
    return Suite(name, project, path, cases, locations)


def only(project: Path, text: str, home: Path, **kwargs):
    suite = suite_of(project, text)
    return run_case(suite, suite.checks[0], home=home, **kwargs)


# -- happy path -------------------------------------------------------------------------------


def test_a_passing_case_reports_no_failures(project: Path, home: Path) -> None:
    result = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect:
      status: succeeded
      exit_code: 0
      outputs: { verdict: LGTM }
      steps:
        review: succeeded
        bail: skipped
""",
        home,
    )
    assert result.ok, result.report()
    assert result.suite == "demo" and result.case == "happy"
    assert result.status == "succeeded"
    assert result.run_id and result.run_dir is not None and result.run_dir.is_dir()


def test_shell_steps_do_not_execute_unless_exec_shell(project: Path, home: Path) -> None:
    """A pure dry run records `shell:` as succeeded with an empty output; --exec-shell runs it."""
    text = """
checks:
  - id: c
    workflow: demo
    stubs: stubs.yaml
    expect: { steps: { note: succeeded }, outputs: { noted: "(dry)" } }
"""
    assert only(project, text, home).ok
    result = only(project, text.replace('"(dry)"', "noted"), home, exec_shell=True)
    assert result.ok, result.report()


def test_a_case_file_cannot_widen_what_run_case_does(project: Path, home: Path) -> None:
    """`exec_shell:` in a data file is a declaration; only the caller authorises execution."""
    text = """
checks:
  - id: c
    workflow: demo
    stubs: stubs.yaml
    exec_shell: true
    expect: { outputs: { noted: "(dry)" } }
"""
    assert only(project, text, home).ok, "case-level exec_shell must not execute by itself"
    authorised = only(project, text.replace('"(dry)"', "noted"), home, exec_shell=True)
    assert authorised.ok, authorised.report()


# -- failures ---------------------------------------------------------------------------------


def test_status_mismatch_names_the_expectation_and_the_line(project: Path, home: Path) -> None:
    result = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect:
      status: failed
""",
        home,
    )
    assert not result.ok
    (failure,) = result.failures
    assert failure.field == "expect.status"
    assert failure.location.endswith("checks.yaml:7"), failure.location
    lines = failure.lines()
    assert len(lines) == 4, lines
    assert lines[0].startswith("expect.status: ")
    assert "succeeded" in lines[0] and "failed" in lines[0]
    assert lines[2].startswith("  fix: ")
    assert lines[3] == f"  at {failure.location}"
    assert failure.location in result.report()


def test_output_mismatch_names_the_output(project: Path, home: Path) -> None:
    result = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect:
      outputs: { verdict: nope }
""",
        home,
    )
    (failure,) = result.failures
    assert failure.field == "expect.outputs.verdict"
    assert "LGTM" in failure.summary


def test_missing_output_lists_the_known_ones(project: Path, home: Path) -> None:
    result = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect:
      outputs: { nope: 1 }
""",
        home,
    )
    (failure,) = result.failures
    assert "verdict" in failure.detail


def test_step_status_mismatch_and_unknown_path(project: Path, home: Path) -> None:
    result = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect:
      steps:
        review: failed
        nope: succeeded
""",
        home,
    )
    fields = {f.field for f in result.failures}
    assert fields == {"expect.steps.review.status", "expect.steps.nope"}
    unknown = next(f for f in result.failures if f.field == "expect.steps.nope")
    assert "review" in unknown.detail  # the known paths are listed


def test_step_output_regex_and_json(project: Path, home: Path) -> None:
    ok = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect:
      steps:
        review: { status: succeeded, output_regex: "LG.M" }
""",
        home,
    )
    assert ok.ok, ok.report()
    bad = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect:
      steps:
        review: { output_regex: "nope" }
""",
        home,
    )
    (failure,) = bad.failures
    assert failure.field == "expect.steps.review.output_regex"
    assert "LGTM" in failure.detail


def test_step_output_json_compares_the_parsed_output(project: Path, home: Path) -> None:
    (project / "stubs_json.yaml").write_text(
        "steps:\n  review: { output: {verdict: ok} }\n", encoding="utf-8"
    )
    result = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs_json.yaml
    expect:
      steps:
        review: { output_json: { verdict: ok } }
""",
        home,
    )
    assert result.ok, result.report()


def test_skip_reason_expectation(project: Path, home: Path) -> None:
    result = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect:
      steps:
        bail: { status: skipped, skip_reason: "totally wrong" }
""",
        home,
    )
    (failure,) = result.failures
    assert failure.field == "expect.steps.bail.skip_reason"


def test_reason_contains(project: Path, home: Path) -> None:
    result = only(
        project,
        """
checks:
  - id: happy
    workflow: demo
    stubs: stubs.yaml
    expect: { reason_contains: "never happens" }
""",
        home,
    )
    (failure,) = result.failures
    assert failure.field == "expect.reason_contains"


# -- load / validate --------------------------------------------------------------------------


def test_unknown_workflow_is_a_case_failure_not_an_exception(project: Path, home: Path) -> None:
    result = only(project, "checks:\n  - {id: c, workflow: nope}\n", home)
    assert not result.ok
    (failure,) = result.failures
    assert failure.field == "workflow"
    assert "nope" in failure.summary


def test_validate_error_is_satisfied_by_a_capability_refusal(project: Path, home: Path) -> None:
    workflow = (project / ".rayspec" / "workflows" / "demo.yaml").read_text(encoding="utf-8")
    (project / ".rayspec" / "workflows" / "bad.yaml").write_text(
        # max_turns is a Claude-only capability: on Codex the loader refuses the workflow
        workflow.replace("name: demo", "name: bad")
        .replace("provider: claude", "provider: codex")
        .replace("    access: read-only", "    access: read-only\n    max_turns: 9"),
        encoding="utf-8",
    )
    result = only(project, "checks:\n  - {id: c, workflow: bad, validate: error}\n", home)
    assert result.ok, result.report()


def test_validate_error_that_does_not_happen_is_a_failure(project: Path, home: Path) -> None:
    result = only(project, "checks:\n  - {id: c, workflow: demo, validate: error}\n", home)
    (failure,) = result.failures
    assert failure.field == "validate"


def test_run_false_stops_after_validation(project: Path, home: Path) -> None:
    result = only(project, "checks:\n  - {id: c, workflow: demo, run: false}\n", home)
    assert result.ok, result.report()
    assert result.run_id is None
    assert result.status == "not run"


def test_a_missing_stubs_file_fails_cleanly(project: Path, home: Path) -> None:
    result = only(project, "checks:\n  - {id: c, workflow: demo, stubs: nope.yaml}\n", home)
    (failure,) = result.failures
    assert failure.field == "stubs"
    assert "not readable" in failure.summary
    assert "Traceback" not in result.report()


def test_an_input_error_is_reported_as_a_failure(project: Path, home: Path) -> None:
    result = only(project, "checks:\n  - {id: c, workflow: demo, inputs: {nope: 1}}\n", home)
    (failure,) = result.failures
    assert failure.field == "inputs"
    assert "nope" in failure.detail


# -- environment ------------------------------------------------------------------------------


def test_env_overrides_apply_and_are_restored(project: Path, home: Path) -> None:
    (project / ".rayspec" / "workflows" / "envdemo.yaml").write_text(
        """
rayspec: 1
name: envdemo
isolation: none
steps:
  - id: peek
    shell: "true"
outputs:
  seen: "{{ env.HARNESS_PROBE | default('unset') }}"
""",
        encoding="utf-8",
    )
    os.environ["HARNESS_PROBE"] = "outer"
    try:
        result = only(
            project,
            """
checks:
  - id: c
    workflow: envdemo
    env: { HARNESS_PROBE: inner }
    expect: { outputs: { seen: inner } }
""",
            home,
        )
        assert result.ok, result.report()
        assert os.environ["HARNESS_PROBE"] == "outer"
    finally:
        os.environ.pop("HARNESS_PROBE", None)


def test_env_null_unsets_a_variable(project: Path, home: Path) -> None:
    (project / ".rayspec" / "workflows" / "envdemo.yaml").write_text(
        """
rayspec: 1
name: envdemo
isolation: none
steps:
  - id: peek
    shell: "true"
outputs:
  seen: "{{ env.HARNESS_PROBE | default('unset') }}"
""",
        encoding="utf-8",
    )
    os.environ["HARNESS_PROBE"] = "outer"
    try:
        result = only(
            project,
            """
checks:
  - id: c
    workflow: envdemo
    env: { HARNESS_PROBE: null }
    expect: { outputs: { seen: unset } }
""",
            home,
        )
        assert result.ok, result.report()
    finally:
        os.environ.pop("HARNESS_PROBE", None)


def test_ambient_input_env_never_feeds_a_case(project: Path, home: Path) -> None:
    """``RAYSPEC_INPUT_*`` in the developer's shell must not change what a case runs with."""
    os.environ["RAYSPEC_INPUT_TARGET"] = "leaked/"
    try:
        result = only(
            project,
            """
checks:
  - id: c
    workflow: demo
    stubs: stubs.yaml
    expect: { status: succeeded }
""",
            home,
        )
        assert result.ok, result.report()
        events = [e for e in result.events if e.type == "run.started"]
        assert events
        assert result.run_dir is not None
        record = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
        assert record["inputs"]["target"] == "src/"
    finally:
        os.environ.pop("RAYSPEC_INPUT_TARGET", None)


def test_cases_do_not_share_a_run_store(project: Path, home: Path) -> None:
    text = "checks:\n  - {id: a, workflow: demo, stubs: stubs.yaml}\n"
    first = only(project, text, home)
    second = only(project, text, home)
    assert first.run_id != second.run_id


def test_result_carries_timing(project: Path, home: Path) -> None:
    """`duration_s` measures the run, and reaches `--json` — a nested window pins both ends."""
    before = time.monotonic()
    result = only(project, "checks:\n  - {id: c, workflow: demo, stubs: stubs.yaml}\n", home)
    after = time.monotonic()
    # Both reads come off the same monotonic clock as the runner's, and the outer interval
    # strictly contains the inner one, so the upper bound holds on every schedule; the lower
    # bound is what catches a `duration_s` that was never written.
    assert 0.0 < result.duration_s <= after - before
    payload = results_json([result], elapsed_s=0.0)
    assert payload["cases"][0]["duration_s"] == round(result.duration_s, 3)


# -- the harness itself never explodes ---------------------------------------------------------


def test_an_unexpected_exception_becomes_a_failure(
    project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_case` is documented as never raising: a bug must not lose the whole suite."""

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr("rayspec.testing.runner.load_config", boom)
    result = only(project, "checks:\n  - {id: c, workflow: demo}\n", home)
    assert not result.ok
    (failure,) = result.failures
    assert failure.field == "internal"
    assert "kaboom" in failure.summary
    assert "RuntimeError" in failure.detail
    assert failure.location.endswith("checks.yaml:2")


def test_an_unreachable_expect_built_in_python_is_a_failure(project: Path, home: Path) -> None:
    """`load_cases` refuses the combination; a hand-built Case must not silently pass either."""
    suite = suite_of(project, "checks:\n  - {id: c, workflow: demo, stubs: stubs.yaml}\n")
    case = suite.checks[0].model_copy(update={"run": False})
    case.expect = Expect(status="this-status-does-not-exist")
    result = run_case(suite, case, home=home)
    assert not result.ok, result.report()
    assert "never evaluated" in result.failures[0].summary


def test_a_passing_case_deletes_its_run_through_the_store(project: Path, home: Path) -> None:
    text = "checks:\n  - {id: c, workflow: demo, stubs: stubs.yaml}\n"
    result = only(project, text, home, keep_run_dir=False)
    assert result.ok, result.report()
    assert result.run_dir is None
    store = FileRunStore(home / "projects" / fallback_project_slug(project))
    assert store.list_runs() == []


def test_the_inputs_file_of_a_case_is_written_privately(
    project: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A case's `inputs:` may hold a value for a `secret: true` input — write it 0600."""
    import rayspec.testing.runner as runner_mod

    modes: list[int] = []
    real = runner_mod.resolve_inputs

    def spy(*args: object, **kwargs: object) -> object:
        path = kwargs.get("inputs_file")
        if isinstance(path, Path):
            modes.append(stat.S_IMODE(path.stat().st_mode))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner_mod, "resolve_inputs", spy)
    result = only(
        project,
        "checks:\n  - {id: c, workflow: demo, stubs: stubs.yaml, inputs: {target: x/}}\n",
        home,
    )
    assert result.ok, result.report()
    assert modes == [0o600], modes


def test_case_environment_clears_an_ambient_policy(tmp_path: Path) -> None:
    """A ``RAYSPEC_POLICY`` a surrounding rayspec run exported must not silently apply to a case:
    ``rayspec test`` isolates each case's environment, and a policy the developer never chose for
    this suite is exactly the kind of ambient state that isolation exists to strip (F4)."""
    from rayspec.testing.runner import case_environment

    os.environ["RAYSPEC_POLICY"] = "/a/leaked/policy.yaml"
    try:
        with case_environment({}, home=tmp_path):
            assert "RAYSPEC_POLICY" not in os.environ
    finally:
        os.environ.pop("RAYSPEC_POLICY", None)


def test_case_environment_keeps_a_policy_the_case_sets(tmp_path: Path) -> None:
    """Clearing the ambient one must not stop a case from choosing its OWN policy via ``env:`` —
    the case's value is applied after the strip and wins."""
    from rayspec.testing.runner import case_environment

    os.environ["RAYSPEC_POLICY"] = "/a/leaked/policy.yaml"
    try:
        with case_environment({"RAYSPEC_POLICY": "/the/case/policy.yaml"}, home=tmp_path):
            assert os.environ["RAYSPEC_POLICY"] == "/the/case/policy.yaml"
    finally:
        os.environ.pop("RAYSPEC_POLICY", None)
