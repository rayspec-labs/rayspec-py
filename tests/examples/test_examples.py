"""Every example and dogfood workflow validates, plans and dry-runs to its expected outcome.

The checks themselves live next to the examples (``checks.yaml``); this module drives them through
``scripts/check_examples.py`` (the same entry point CI uses) and verifies the coverage matrix in
``examples/README.md``.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"
SCRIPT = REPO_ROOT / "scripts" / "check_examples.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_examples", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve string annotations via sys.modules
    spec.loader.exec_module(module)
    return module


check_examples = _load_script()

SUITES = check_examples.discover_suites(REPO_ROOT)
CHECKS = [(suite, check) for suite in SUITES for check in suite.checks]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated ``RAYSPEC_HOME`` so dry runs never touch the developer's store."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("RAYSPEC_HOME", str(path))
    return path


def test_suites_discovered() -> None:
    names = {suite.name for suite in SUITES}
    assert {
        "hello_review",
        "fix_issue",
        "triage_fanout",
        "pr_review",
        "unsupported_demo",
        "release_check",
        "dogfood",
    } <= names
    assert all(suite.checks for suite in SUITES), "every suite declares at least one check"


@pytest.mark.parametrize(
    ("suite", "check"),
    CHECKS,
    ids=[f"{suite.name}:{check.id}" for suite, check in CHECKS],
)
def test_check_passes(suite, check, home: Path) -> None:
    result = check_examples.run_check(suite, check, home=home)
    assert result.ok, result.report()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def detached_dogfood(tmp_path: Path) -> object:
    """The repo's ``.rayspec/`` tree in a fresh git repo on a detached HEAD (what CI checks out)."""
    root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / ".rayspec", root / ".rayspec")
    _git("init", "-q", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "dogfood", cwd=root)
    _git("checkout", "-q", "--detach", cwd=root)
    checks_path = root / ".rayspec" / "dryrun" / "checks.yaml"
    return check_examples.Suite(
        "dogfood", root, checks_path, check_examples.load_checks(checks_path)
    )


@pytest.mark.parametrize(
    "check_id", [c.id for s in SUITES if s.name == "dogfood" for c in s.checks]
)
def test_dogfood_checks_pass_on_detached_head(detached_dogfood, check_id: str, home: Path) -> None:
    """``run.branch`` is None on a detached HEAD; the dogfood workflows must still dry-run."""
    check = next(c for c in detached_dogfood.checks if c.id == check_id)
    result = check_examples.run_check(detached_dogfood, check, home=home)
    assert result.ok, result.report()


def test_every_example_has_readme_and_checks() -> None:
    for suite in SUITES:
        if suite.name == "dogfood":
            continue
        assert (suite.root / "README.md").is_file(), suite.name
        assert (suite.root / "checks.yaml").is_file(), suite.name
        assert (suite.root / ".rayspec" / "workflows").is_dir(), suite.name


def test_dogfood_workflows_present() -> None:
    workflows = {p.stem for p in (REPO_ROOT / ".rayspec" / "workflows").glob("*.yaml")}
    assert {"review_pr", "fix_issue", "implement_feature_tdd", "docs_sync", "release_check"} <= (
        workflows
    )


# --------------------------------------------------------------------------------------------------
# Coverage matrix
# --------------------------------------------------------------------------------------------------

#: Every capability of the workflow language (docs/schema.md) + the CLI surface must appear
#: as a row in the matrix of examples/README.md.
REQUIRED_ROWS = [
    # step kinds
    "prompt:",
    "prompt_file:",
    "shell:",
    "python:",
    "loop:",
    "each:",
    "approve:",
    "include:",
    "stop:",
    # common fields
    "description",
    "needs:",
    "when:",
    "join: all",
    "join: any",
    "join: always",
    "timeout:",
    "retry:",
    "always_run:",
    "allow_failure:",
    "env:",
    "output_schema:",
    "interpreter:",
    "cwd:",
    "deps:",
    "session:",
    "max_iterations",
    "until:",
    "on_exhausted",
    "as:",
    "max_parallel",
    "on_failure",
    "on_reject",
    "with:",
    "outputs:",
    "defaults.agent",
    "defaults.timeout",
    "defaults.max_parallel",
    "defaults.on_unsupported",
    "defaults.on_step_failure",
    "isolation: none",
    "isolation: worktree",
    # templating
    "inputs.*",
    "steps.<id>.output",
    "steps.<id>.ok",
    "steps.<id>.exit_code",
    "steps.<id>.items",
    "steps.<id>.iterations",
    "run.*",
    "project.*",
    "iteration.n",
    "iteration.prev",
    "each.index",
    "env.<VAR>",
    "fromjson",
    "regex_search",
    "has_signal",
    "default(",
    "{% raw %}",
    "${RAYSPEC_V",
    "RAYSPEC_INPUT_",
    "RAYSPEC_CONTEXT",
    "RAYSPEC_ARTIFACTS_DIR",
    # inputs
    "type: string",
    "type: integer",
    "type: number",
    "type: boolean",
    "type: array",
    "type: object",
    "enum",
    "required: true",
    "default",
    # agents
    "provider: claude",
    "provider: codex",
    "model: <tier>",
    "model: <literal>",
    'model: "@<alias>"',
    "effort",
    "access: read-only",
    "access: workspace-write",
    "access: full",
    "instructions",
    "instructions_file",
    "instructions_mode",
    "max_turns",
    "budget_usd",
    "tools.allow",
    "tools.deny",
    "thinking",
    "mcp",
    "provider_options",
    "agent: {extends",
    "inline agent",
    ".rayspec/agents/",
    "workflow `agents:`",
    # config
    "config.yaml tiers",
    "config.yaml aliases",
    "config.yaml pricing",
    "config.yaml providers",
    # CLI
    "rayspec validate",
    "rayspec plan",
    "rayspec run",
    "rayspec workflows",
    "rayspec agents",
    "rayspec providers",
    "rayspec projects",
    "rayspec worktrees",
    "--input",
    "--inputs-file",
    "--dry-run",
    "--stubs",
    "--stubs-init",
    "--exec-shell",
    "--yes",
    "--no-interactive",
    "--json",
    "--quiet",
    "--verbose",
    "--allow-unsupported",
    "--fail-fast",
    "--resume",
    "--force",
    "--no-worktree",
    "--base",
    "--repo",
    "--root",
    # stubs file
    "stubs: sequence",
    "stubs: fail",
    "stubs: match",
    "stubs: output",
]


def _matrix_rows() -> dict[str, list[str]]:
    return check_examples.parse_coverage_matrix(EXAMPLES_DIR / "README.md")


def test_coverage_matrix_rows_have_examples() -> None:
    rows = _matrix_rows()
    assert rows, "examples/README.md has no coverage matrix table"
    known = {suite.name for suite in SUITES}
    for capability, examples in rows.items():
        assert examples, f"matrix row {capability!r} names no example"
        for name in examples:
            assert name in known, f"matrix row {capability!r} names unknown example {name!r}"


def test_coverage_matrix_is_complete() -> None:
    rows = _matrix_rows()
    keys = list(rows)
    missing = [req for req in REQUIRED_ROWS if not any(req in key for key in keys)]
    assert not missing, f"coverage matrix lacks rows for: {missing}"


def test_matrix_claims_are_backed_by_files() -> None:
    """Every token of a row (`--flag`, `RAYSPEC_*`, `rayspec <cmd>`, `key:` …) occurs in at least one
    named example, and every named example backs at least one token of the row. Dogfood rows scan
    only ``.rayspec/``; comment-only YAML lines do not count."""
    problems = check_examples.unbacked_claims(_matrix_rows(), SUITES)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("capability", "needles"),
    [
        ("`--fail-fast`", ["\\-\\-fail\\-fast"]),
        (
            "`rayspec projects add\\|list\\|remove`",
            [
                "rayspec\\ projects\\ add",
                "rayspec\\ projects\\ list",
                "rayspec\\ projects\\ remove",
            ],
        ),
        ("`rayspec run <wf>`", ["rayspec\\ run"]),
        ("`RAYSPEC_INPUT_<NAME>`", ["RAYSPEC_INPUT_\\S+"]),
        ("`stubs: sequence` (per entry)", ["sequence:"]),
        ("`config.yaml tiers`", ["tiers:"]),
        ("`defaults.on_unsupported`", ["on_unsupported:"]),
        ("`{% raw %}` for Go-template braces", ["\\{%\\ raw"]),
        ("`{{ }}` in `shell:`", ["shell:"]),
    ],
)
def test_matrix_needles(capability: str, needles: list[str]) -> None:
    assert check_examples.matrix_needles(capability) == needles


def test_unbacked_claims_flags_missing_and_comment_only(tmp_path: Path) -> None:
    root = tmp_path / "ex"
    (root / ".rayspec" / "workflows").mkdir(parents=True)
    (root / "README.md").write_text("run `rayspec run wf --json`\n")
    (root / ".rayspec" / "workflows" / "wf.yaml").write_text(
        "# defaults:\n#   on_unsupported: warn\nsteps: []\n"
    )
    suite = check_examples.Suite("ex", root, root / "checks.yaml", ())
    other = check_examples.Suite("other", root, root / "checks.yaml", ())
    rows = {
        "`--json`": ["ex"],
        "`--verbose`": ["ex"],
        "`defaults.on_unsupported`": ["ex"],
        "`steps:`": ["ex", "other"],
    }
    problems = check_examples.unbacked_claims(rows, [suite, other])
    assert any("'`--verbose`'" in p for p in problems)
    assert any("on_unsupported" in p for p in problems)
    assert not any("--json" in p for p in problems)
    # `other` shares the tree here, so its attribution is backed too
    assert not any("steps:" in p for p in problems)


# --------------------------------------------------------------------------------------------------
# scripts/check_examples.py itself
# --------------------------------------------------------------------------------------------------


def test_only_unknown_suite_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """A typo in ``--only`` must not pass silently as 'all checks passed'."""
    assert check_examples.main(["--only", "nope"]) == check_examples.EXIT_USAGE
    err = capsys.readouterr().err
    assert "nope" in err
    assert "hello_review" in err and "dogfood" in err


def test_expect_unknown_keys_are_named(tmp_path: Path) -> None:
    """A typo in a case file names the key, the suggestion and the line (docs/testing.md)."""
    checks = tmp_path / "checks.yaml"
    checks.write_text("checks:\n  - workflow: wf\n    expect: {bogus: 1, status: ok}\n")
    with pytest.raises(check_examples.CheckFileError) as excinfo:
        check_examples.load_checks(checks)
    assert "unknown field 'bogus' for expect" in str(excinfo.value)
    assert "checks.yaml:3" in str(excinfo.value)


def test_check_env_is_parsed(tmp_path: Path) -> None:
    checks = tmp_path / "checks.yaml"
    checks.write_text("checks:\n  - workflow: wf\n    env: {A: '1', B: null}\n")
    (check,) = check_examples.load_checks(checks)
    assert check.env == {"A": "1", "B": None}


def test_release_check_push_ignores_host_slack_webhook(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `push` scenario must not depend on whether the developer exports SLACK_WEBHOOK."""
    monkeypatch.setenv("SLACK_WEBHOOK", "https://hooks.example.invalid/x")
    suite = next(s for s in SUITES if s.name == "release_check")
    check = next(c for c in suite.checks if c.id == "push")
    result = check_examples.run_check(suite, check, home=home)
    assert result.ok, result.report()


def _walk(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_no_workflow_env_squats_on_the_rayspec_prefix() -> None:
    """``RAYSPEC_*`` is the engine's namespace (``RAYSPEC_V<n>`` slots overwrite step ``env:``)."""
    workflows = [
        *EXAMPLES_DIR.glob("*/.rayspec/workflows/*.yaml"),
        *(REPO_ROOT / ".rayspec" / "workflows").glob("*.yaml"),
    ]
    assert workflows
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: env.{name}"
        for path in workflows
        for mapping in _walk(check_examples.yaml.safe_load(path.read_text(encoding="utf-8")))
        if isinstance(mapping.get("env"), dict)
        for name in mapping["env"]
        if str(name).startswith("RAYSPEC_")
    ]
    assert not offenders, offenders


# --------------------------------------------------------------------------------------------------
# --json stdout contract (checked for every dry run)
# --------------------------------------------------------------------------------------------------

_SUMMARY = (
    '{"run_id": "r1", "status": "succeeded", "exit_code": 0, "reason": null, "outputs": {}, '
    '"usage": {}, "cost_usd": null, "cost_source": "none", "run_dir": "/x", "workspace": {}, '
    '"pause": null}'
)
_EVENT = (
    '{"type": "%s", "run_id": "r1", "ts": "t", "step_path": null, "data": {"status": "succeeded"}}'
)


def test_json_stream_problems_accepts_the_contract() -> None:
    stdout = "\n".join([_EVENT % "run.started", _EVENT % "run.finished", _SUMMARY, ""])
    assert check_examples.json_stream_problems(stdout) == []


def test_json_stream_problems_flags_summary_not_last_and_bad_keys() -> None:
    swapped = "\n".join([_EVENT % "run.started", _SUMMARY, _EVENT % "run.finished"])
    assert any("summary" in p for p in check_examples.json_stream_problems(swapped))
    extra = (
        '{"type": "run.started", "run_id": "r1", "ts": "t", "step_path": null, "data": {}, "x": 1}'
    )
    stdout = "\n".join([extra, _EVENT % "run.finished", _SUMMARY])
    assert any("keys" in p for p in check_examples.json_stream_problems(stdout))
    noise = "\n".join(
        [_EVENT % "run.started", "run demo succeeded", _EVENT % "run.finished", _SUMMARY]
    )
    assert any("not JSON" in p for p in check_examples.json_stream_problems(noise))
    no_finish = "\n".join([_EVENT % "run.started", _SUMMARY])
    assert any("run.finished" in p for p in check_examples.json_stream_problems(no_finish))


def test_json_stream_problems_reports_null_finished_data_instead_of_raising() -> None:
    """A malformed ``run.finished`` (``data: null``) is a reported problem, never an exception."""
    finished = (
        '{"type": "run.finished", "run_id": "r1", "ts": "t", "step_path": null, "data": null}'
    )
    stdout = "\n".join([_EVENT % "run.started", finished, _SUMMARY])
    problems = check_examples.json_stream_problems(stdout)
    assert any("run.finished" in p and "summary" in p for p in problems), problems


def test_summary_keys_come_from_the_cli() -> None:
    from rayspec.cli.commands.run import SUMMARY_KEYS

    assert check_examples._SUMMARY_KEYS is SUMMARY_KEYS


def test_run_check_reports_a_missing_stubs_file_cleanly(tmp_path: Path, home: Path) -> None:
    """A missing ``stubs:`` file is a located failure, never a traceback."""
    suite = next(s for s in SUITES if s.name == "hello_review")
    base = next(c for c in suite.checks if c.run)
    check = check_examples.Check(
        id="missing-stubs",
        workflow=base.workflow,
        inputs=base.inputs,
        stubs=tmp_path / "nope.yaml",
        expect=check_examples.Expect(status="succeeded", exit_code=0),
    )
    result = check_examples.run_check(suite, check, home=home)
    assert not result.ok, result.report()
    (failure,) = result.failures
    assert failure.field == "stubs"
    assert "stubs file not readable" in failure.summary
    assert "nope.yaml" in failure.summary
    report = result.report()
    assert "Traceback" not in report and "FileNotFoundError" not in report


def test_every_suite_gets_a_cli_json_contract_smoke(home: Path) -> None:
    """The stdout contract stays in the gate: one case per suite is driven through the real
    CLI with --json.

    The harness behind ``rayspec test`` runs the engine directly, so nothing else in this script
    would notice the summary object leaving stdout (or an event growing a key).
    """
    for suite in SUITES:
        assert check_examples.smoke_case(suite) is not None, suite.name
    suite = next(s for s in SUITES if s.name == "hello_review")
    names = [r.case for r in check_examples._iter_results([suite], home=home, only=None)]
    assert any(name.endswith("(cli --json)") for name in names), names
    smoke = check_examples.cli_contract_check(suite, check_examples.smoke_case(suite), home=home)
    assert smoke.ok, smoke.report()


def test_the_cli_contract_smoke_catches_a_summary_that_left_stdout() -> None:
    """The check itself is the pin: the summary object must be the last stdout line."""
    stdout = _EVENT % "run.finished" + "\n"  # summary went elsewhere (stderr)
    assert any("summary" in p for p in check_examples.json_stream_problems(stdout))
