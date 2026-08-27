"""The bundled ``prd_to_pr`` workflow against a real tree.

Two nets, both on a throw-away repository with ``app.py``, a dependency-free test runner, a
syntax check standing in for a typechecker, a small PRD under ``docs/``, a bare ``origin`` to push
into and a ``gh`` first on ``PATH`` that records ``pr create`` instead of talking to GitHub:

* ``prd_to_pr/checks.yaml`` — declarative cases driven through ``rayspec test --exec-shell`` with
  the stub provider, which never edits a file: the PRD's settings block wins over an input, too
  many requirements and missing acceptance criteria refuse the run, a red baseline stops it, a
  test writer that wrote nothing is caught by ``red``, a held ``scope`` class pauses at the gate.
* in-process runs with :class:`PrdProvider` standing in for ``claude`` — agents that really write
  ``test_total.py`` and edit ``app.py``, which is the only way to see the tests proven red, the
  loop converge (first or third try, with the two distinct briefs), vacuous tests fail the run
  before any implementation, the give-up bound retain the work, the tamper guard catch an
  implementer that edits the acceptance tests, and a question answered at the plan gate reach the
  test writer, the implementer and the PR body.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.config import Config
from rayspec.engine.approval import ApprovalAnswer, ApprovalRequest
from rayspec.engine.approval_classes import ApprovalClasses, ClassRules
from rayspec.engine.context import RunOptions
from rayspec.engine.runner import Runner, RunResult, Workspace
from rayspec.loader import load_workflow
from rayspec.loader.inputs import resolve_inputs
from rayspec.providers.base import (
    AgentError,
    AgentRequest,
    AgentResult,
    EmitFn,
    ProviderHealth,
)
from rayspec.providers.stub import STUB_CAPABILITIES
from rayspec.store.file import FileRunStore
from rayspec.testing import load_checks

HERE = Path(__file__).resolve().parent
SUITE = HERE / "prd_to_pr"
CASES = load_checks(SUITE / "checks.yaml")
runner = CliRunner()

GIT_ENV = {
    "GIT_AUTHOR_NAME": "rayspec-test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "rayspec-test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

APP = """\
def helper():
    return 1
"""
#: what the PRD asks for
APP_TOTAL = (
    APP
    + """

def total(xs):
    return sum(xs)
"""
)
APP_BROKEN = APP + "\n\ndef total(xs) return sum(xs)\n"
APP_WRONG = APP_TOTAL.replace("return sum(xs)", "return sum(xs) + 1")

TEST_APP = """\
import app


def test_helper():
    assert app.helper() == 1
"""
TEST_APP_RED = TEST_APP.replace("== 1", "== 2")
#: the acceptance tests the fake tester writes: red until app.total exists and is right
TEST_TOTAL = """\
import app


def test_total_sums():
    assert app.total([1, 2, 3]) == 6


def test_total_empty():
    assert app.total([]) == 0
"""
#: a test that passes without any implementation
TEST_VACUOUS = """\
def test_total():
    assert True
"""

#: a dependency-free runner: every test_*.py, every test_* function, pytest's short-summary shape
RUN_TESTS = """\
import importlib
import pathlib
import sys

failed = passed = 0
for path in sorted(pathlib.Path(".").glob("test_*.py")):
    try:
        module = importlib.import_module(path.stem)
    except Exception as exc:  # noqa: BLE001 - any import error is a failed file
        failed += 1
        print(f"FAILED {path.name} - {type(exc).__name__}: {exc}")
        continue
    for name in sorted(dir(module)):
        function = getattr(module, name)
        if name.startswith("test_") and callable(function):
            try:
                function()
                passed += 1
            except Exception as exc:  # noqa: BLE001 - any error is a failed test
                failed += 1
                print(f"FAILED {path.name}::{name} - {type(exc).__name__}: {exc}")
print(f"{failed} failed, {passed} passed")
sys.exit(1 if failed else 0)
"""
#: a syntax check standing in for a typechecker (nothing is imported, no .pyc is written)
TYPECHECK = """\
import ast
import pathlib
import sys

bad = 0
for path in sorted(pathlib.Path(".").glob("*.py")):
    try:
        ast.parse(path.read_text(encoding="utf-8"), str(path))
    except SyntaxError as exc:
        bad += 1
        print(f"{path}:{exc.lineno}: SyntaxError: {exc.msg}")
print("typecheck ok" if not bad else f"{bad} file(s) do not parse")
sys.exit(1 if bad else 0)
"""

TITLE = "Sum of a list"
PRD = f"""\
# {TITLE}

<!-- rayspec
test_command: python3 -B run_tests.py
typecheck: python3 -B typecheck.py
max_requirements: 6
-->

## Requirements

**R1 —** `app.total(xs)` returns the sum of a list of integers.

**R2 —** `app.total([])` returns 0.

## Acceptance criteria

- `total([1, 2, 3])` is 6.
- `total([])` is 0.
"""
PRD_BIG = PRD.replace(
    "**R2 —** `app.total([])` returns 0.\n",
    "".join(f"**R{n} —** requirement {n}.\n\n" for n in range(2, 21)),
)
PRD_NO_CRITERIA = PRD.split("## Acceptance criteria")[0]

TEST_COMMAND = "python3 -B run_tests.py"
TYPECHECK_COMMAND = "python3 -B typecheck.py"
PLAN: dict[str, Any] = {
    "summary": "add app.total",
    "test_plan": [
        {"requirement": "R1 — app.total(xs) returns the sum", "tests": ["test_total_sums"]},
        {"requirement": "R2 — app.total([]) returns 0", "tests": ["test_total_empty"]},
    ],
    "unresolved": [],
}
QUESTION = "Should total accept floats?"
PLAN_WITH_QUESTION: dict[str, Any] = {
    **PLAN,
    "unresolved": [{"question": QUESTION, "assumption": "integers only", "affects": ["R1"]}],
}
COMPLETE: dict[str, Any] = {
    "covered": ["R1", "R2"],
    "uncovered": [],
    "unrequested": [],
    "verdict": "complete",
    "summary": "total() sums a list and returns 0 for an empty one",
}
PARTIAL: dict[str, Any] = {
    "covered": ["R1"],
    "uncovered": ["R2"],
    "unrequested": ["logging"],
    "verdict": "partial",
    "summary": "R2 is not covered and a logger was added",
}
#: what the workflow's header asks operators for: `scope` is never approved automatically
SCOPE_HELD = ApprovalClasses(rules={"scope": ClassRules(allow_yes=False)}, policy_loaded=True)

FAKE_GH = """#!/bin/sh
# Records every invocation (and the body of `pr create`) in $GH_LOG; fails the subcommand named
# by $GH_FAIL the way a real `gh` would (non-zero exit, a line on stderr).
printf '%s\\n' "gh $*" >> "$GH_LOG"
if [ -n "${GH_FAIL:-}" ] && [ "$2" = "$GH_FAIL" ]; then
  echo "gh: simulated $GH_FAIL failure" >&2
  exit 1
fi
case "$1 $2" in
  "pr create")
    cat >> "$GH_LOG"
    printf '\\n--- end of body\\n' >> "$GH_LOG"
    echo "https://github.com/example/repo/pull/99" ;;
esac
exit 0
"""
PR_URL = "https://github.com/example/repo/pull/99"


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(root: Path, *, prd: str = PRD, tests: str = TEST_APP) -> Path:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "app.py", APP)
    write(root, "test_app.py", tests)
    write(root, "run_tests.py", RUN_TESTS)
    write(root, "typecheck.py", TYPECHECK)
    write(root, "docs/prd.md", prd)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def big_repo(tmp_path: Path, home: Path) -> Path:
    return make_repo(tmp_path / "big", prd=PRD_BIG)


@pytest.fixture
def nocriteria_repo(tmp_path: Path, home: Path) -> Path:
    return make_repo(tmp_path / "nocriteria", prd=PRD_NO_CRITERIA)


@pytest.fixture
def red_repo(tmp_path: Path, home: Path) -> Path:
    """The same tree with a test that fails before anything is touched."""
    return make_repo(tmp_path / "red", tests=TEST_APP_RED)


def add_origin(repo: Path) -> Path:
    """A bare repository the workflow pushes into; ``main`` is already there."""
    origin = repo.parent / f"{repo.name}-origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    return origin


def remote_branches(origin: Path) -> list[str]:
    return git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()


def install_fake_gh(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``gh`` that logs instead of talking to GitHub; returns the log file."""
    bin_dir = root.parent / f"{root.name}-bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(FAKE_GH, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = bin_dir / "gh.log"
    # a case's `env:` is a literal string and cannot name this directory: the driver sets both
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.delenv("GH_FAIL", raising=False)
    return log


# --------------------------------------------------------------------------------------------------
# rayspec test --exec-shell over prd_to_pr/checks.yaml
# --------------------------------------------------------------------------------------------------


def _run_case(root: Path, case_id: str, monkeypatch: pytest.MonkeyPatch) -> str:
    shutil.copy(SUITE / "checks.yaml", root / "checks.yaml")
    shutil.copytree(SUITE / "stubs", root / "stubs")
    shutil.copytree(SUITE / "policy", root / "policy")
    git(root, "add", "-A")  # the suite is part of the tree, so a clean status means "untouched"
    git(root, "commit", "-q", "-m", "suite")
    monkeypatch.chdir(root)  # a case's RAYSPEC_POLICY is relative to the cwd
    # `rayspec test` reads the operator policy once per suite root, at start-up — before a case's
    # own `env:` is applied — so the policy a case names has to be in force before the command runs.
    (case,) = [c for c in CASES if c.id == case_id]
    if policy := case.env.get("RAYSPEC_POLICY"):
        monkeypatch.setenv("RAYSPEC_POLICY", policy)
    res = runner.invoke(app, ["test", "--root", str(root), "--exec-shell", "--case", case_id])
    assert res.exit_code == 0, res.output
    assert f"ok checks:{case_id}" in res.output, res.output
    return res.output


FIXTURE_FOR = {
    "oversized": "big_repo",
    "no_criteria": "nocriteria_repo",
    "red_baseline": "red_repo",
}


@pytest.mark.parametrize("case_id", [case.id for case in CASES])
def test_every_real_tree_case_passes(
    request: pytest.FixtureRequest, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = request.getfixturevalue(FIXTURE_FOR.get(case_id, "repo"))
    origin = add_origin(root)
    log = install_fake_gh(root, monkeypatch)
    _run_case(root, case_id, monkeypatch)
    # none of these cases writes, commits, pushes or opens anything
    assert git(root, "log", "-1", "--format=%s") == "suite"
    assert git(root, "status", "--porcelain") == ""
    assert remote_branches(origin) == ["main"]
    assert not log.exists()


# --------------------------------------------------------------------------------------------------
# in-process: agents that really write tests and code
# --------------------------------------------------------------------------------------------------

Edit = Callable[[Path], None]


def writes(name: str, text: str) -> Edit:
    def edit(cwd: Path) -> None:
        write(cwd, name, text)

    return edit


class PrdProvider:
    """A ``Provider`` for ``claude``: the planner returns ``plan``, the tester applies ``tests`` to
    the run's working directory, every implementer call pops the next scripted edit, the reviewer
    returns ``review`` (``None`` = a provider outage). Prompts are recorded by role — the schema a
    request carries says which agent is asking (the tester has none)."""

    id = "claude"
    capabilities = STUB_CAPABILITIES

    def __init__(
        self,
        *,
        edits: list[Edit],
        plan: dict[str, Any] | None = None,
        tests: Edit | None = None,
        review: dict[str, Any] | None = COMPLETE,
        scout_questions: list[str] | None = None,
    ) -> None:
        self.plan = plan or PLAN
        self.tests = tests or writes("test_total.py", TEST_TOTAL)
        self.edits = list(edits)
        self.review = review
        # default: no questions → the explore fan-out is a no-op, so tests that do not care
        # about it keep the same planner/tester/implementer/reviewer call counts they had in v1
        self.scout_questions = list(scout_questions or [])
        self.plan_prompts: list[str] = []
        self.test_prompts: list[str] = []
        self.implement_prompts: list[str] = []
        self.implement_sessions: list[str | None] = []
        self.review_prompts: list[str] = []
        self.scout_prompts: list[str] = []
        self.explorer_prompts: list[str] = []

    async def open(
        self, *, run_id: str, workdir: str, env: Mapping[str, str], max_parallel: int
    ) -> None:
        return None

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
        return ProviderHealth(ok=True, sdk_version=None, auth="ok", details=())

    async def aclose(self) -> None:
        return None

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        properties = (req.output_schema or {}).get("properties", {})
        if "unresolved" in properties:  # the planner
            self.plan_prompts.append(req.prompt)
            return AgentResult(status="success", text=json.dumps(self.plan), structured=self.plan)
        if "questions" in properties:  # the scout (names the explorer questions)
            self.scout_prompts.append(req.prompt)
            out = {"questions": list(self.scout_questions)}
            return AgentResult(status="success", text=json.dumps(out), structured=out)
        if "reuse" in properties:  # an explorer (answers exactly one question, read-only)
            self.explorer_prompts.append(req.prompt)
            out = {
                "question": "explored",
                "answer": "read the code",
                "files": [{"path": "app.py", "why": "the module"}],
                "reuse": [],
                "conventions": [],
                "risks": [],
            }
            return AgentResult(status="success", text=json.dumps(out), structured=out)
        if "question" in properties:  # the implementer
            self.implement_prompts.append(req.prompt)
            self.implement_sessions.append(req.resume_session)
            self.edits.pop(0)(Path(req.cwd))
            done = {"status": "done", "question": "", "notes": "edited app.py"}
            return AgentResult(
                status="success",
                text=json.dumps(done),
                structured=done,
                session_ref=f"impl-{len(self.implement_prompts)}",
            )
        if "verdict" in properties:  # the reviewer
            self.review_prompts.append(req.prompt)
            if self.review is None:
                error = AgentError(kind="api", message="simulated outage", transient=False)
                return AgentResult(status="error", text="", error=error)
            return AgentResult(
                status="success", text=json.dumps(self.review), structured=self.review
            )
        self.test_prompts.append(req.prompt)  # the tester: no schema
        self.tests(Path(req.cwd))
        return AgentResult(status="success", text="Wrote the acceptance tests; they fail.")


class Answering:
    """An ``ApprovalPrompt`` that records what it was shown and answers from a script."""

    def __init__(self, *answers: ApprovalAnswer) -> None:
        self.answers = list(answers)
        self.requests: list[ApprovalRequest] = []

    async def __call__(self, request: ApprovalRequest) -> ApprovalAnswer | None:
        self.requests.append(request)
        return self.answers.pop(0)


async def run_in_place(
    repo: Path,
    home: Path,
    provider: PrdProvider,
    *,
    options: RunOptions | None = None,
    approval_prompt: Answering | None = None,
    **inputs: Any,
) -> RunResult:
    resolved = load_workflow("prd_to_pr", project_root=repo, home=home, config=Config())
    given = {
        "prd": "docs/prd.md",
        "typecheck_command": TYPECHECK_COMMAND,
        "test_command": TEST_COMMAND,
        **inputs,
    }
    return await Runner(
        resolved,
        # what the CLI does before it builds a Runner: defaults filled in, values coerced
        inputs=resolve_inputs(
            resolved.workflow, cli_pairs=[f"{name}={value}" for name, value in given.items()]
        ),
        store=FileRunStore(home / "store"),
        project_root=repo,
        project_slug="local/test",
        workspace=Workspace.in_place(repo),
        providers={"claude": provider},
        approval_prompt=approval_prompt,
        # a real run: the gates need an answer — `yes`, which a held class still refuses
        options=options or RunOptions(yes=True),
        handle_signals=False,
    ).run()


@pytest.fixture
def publishable(repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """The repository with a bare ``origin`` and a fake ``gh``: ``(repo, origin, gh log)``."""
    return repo, add_origin(repo), install_fake_gh(repo, monkeypatch)


def short_id(result: RunResult) -> str:
    return result.run_id.rsplit("-", 1)[-1]


TESTS_COMMIT = f"test: acceptance tests for {TITLE}"
IMPL_COMMIT = f"feat: {TITLE}"


@pytest.mark.anyio
async def test_converges_first_try_and_opens_a_pr(
    publishable: tuple[Path, Path, Path], home: Path
) -> None:
    repo, origin, log = publishable
    provider = PrdProvider(edits=[writes("app.py", APP_TOTAL)])
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None
    branch = f"prd/prd-{short_id(result)}"
    assert result.outputs["attempts"] == 1
    assert result.outputs["verdict"] == "complete"
    assert result.outputs["tests"] == ["test_total.py"]
    assert result.outputs["branch"] == branch
    assert result.outputs["pr_url"] == PR_URL
    # the tests-first history, and the branch under its PRD-derived name on the remote
    assert git(repo, "log", "--format=%s").splitlines() == [IMPL_COMMIT, TESTS_COMMIT, "init"]
    assert git(repo, "status", "--porcelain") == ""
    assert remote_branches(origin) == ["main", branch]
    assert git(origin, "rev-parse", branch) == git(repo, "rev-parse", "HEAD")
    logged = log.read_text(encoding="utf-8")
    assert f"gh pr create --base main --head {branch} --title {TITLE} --body-file -\n" in logged
    assert COMPLETE["summary"] in logged
    assert "## Coverage (reviewer's verdict: complete)\n\ncovered:\n- R1\n- R2\n" in logged
    assert "## Assumptions\n\n- (none)\n" in logged
    assert "The plan gate was approved automatically" in logged
    # the code proved the tests red before the implementer was asked
    assert result.steps["red"].status == "succeeded"
    (implement,) = provider.implement_prompts
    assert (
        "The acceptance tests (test_total.py) fail right now\n"
        "(`python3 -B run_tests.py` exited 1):\n"
    ) in implement
    assert (
        "FAILED test_total.py::test_total_sums - AttributeError: module 'app' has no attribute 'total'"
        in implement
    )
    (review,) = provider.review_prompts
    assert "+def total(xs):\n+    return sum(xs)\n" in review
    assert "The diff (acceptance tests: test_total.py):" in review
    assert "pass after 1 attempt(s)." in review


@pytest.mark.anyio
async def test_converges_on_the_third_try_with_distinct_briefs(
    publishable: tuple[Path, Path, Path], home: Path
) -> None:
    repo, origin, _log = publishable
    provider = PrdProvider(
        edits=[
            writes("app.py", APP_BROKEN),
            writes("app.py", APP_WRONG),
            writes("app.py", APP_TOTAL),
        ]
    )
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None and result.outputs["attempts"] == 3
    _first, second, third = provider.implement_prompts
    assert second.startswith("Attempt 2 of 4 (session continued).\n")
    assert (
        "The tree does not typecheck after your last edit (`python3 -B typecheck.py` exited 1):\n"
        "app.py:5: SyntaxError:"
    ) in second
    assert "The tests fail too (`python3 -B run_tests.py` exited 1):\n" in second
    assert third.startswith("Attempt 3 of 4 (session continued).\n")
    assert "does not typecheck" not in third
    assert (
        "The tree typechecks, but the tests fail (`python3 -B run_tests.py` exited 1):\n" in third
    )
    assert "FAILED test_total.py::test_total_sums - AssertionError" in third
    # the implementer keeps its own session across attempts; the first call is fresh
    assert provider.implement_sessions == [None, "impl-1", "impl-2"]
    assert remote_branches(origin) == ["main", f"prd/prd-{short_id(result)}"]


@pytest.mark.anyio
async def test_vacuous_tests_fail_the_run_before_any_implementation(
    publishable: tuple[Path, Path, Path], home: Path
) -> None:
    repo, origin, log = publishable
    provider = PrdProvider(
        edits=[writes("app.py", APP_TOTAL)], tests=writes("test_total.py", TEST_VACUOUS)
    )
    result = await run_in_place(repo, home, provider)
    assert result.status == "failed", result.reason
    assert result.exit_code == 1
    assert result.reason is not None
    assert (
        "no new test fails against the current code — vacuous tests: test_total.py" in result.reason
    )
    assert result.steps["vacuous"].status == "succeeded"
    assert result.steps["commit_tests"].status == "skipped"
    assert provider.implement_prompts == []
    assert git(repo, "log", "-1", "--format=%s") == "init"
    assert remote_branches(origin) == ["main"]
    assert not log.exists()


@pytest.mark.anyio
async def test_exhaustion_fails_and_retains_the_work(
    publishable: tuple[Path, Path, Path], home: Path
) -> None:
    repo, origin, log = publishable
    provider = PrdProvider(edits=[writes("app.py", APP_WRONG), writes("app.py", APP_WRONG)])
    result = await run_in_place(repo, home, provider, max_attempts=2)
    assert result.status == "failed", result.reason
    assert result.reason is not None
    assert "not green after 2 done attempt(s) — typecheck exited 0, tests exited 1" in result.reason
    assert result.steps["build[2]/give_up"].status == "succeeded"
    assert len(provider.implement_prompts) == 2
    assert provider.review_prompts == []  # nothing to review: the loop never went green
    # the tests are committed, the last attempt is left in the tree, nothing was pushed
    assert git(repo, "log", "-1", "--format=%s") == TESTS_COMMIT
    assert git(repo, "status", "--porcelain") != ""
    assert (repo / "app.py").read_text(encoding="utf-8") == APP_WRONG
    assert remote_branches(origin) == ["main"]
    assert not log.exists()


@pytest.mark.anyio
async def test_tampering_with_the_acceptance_tests_fails_the_run(
    publishable: tuple[Path, Path, Path], home: Path
) -> None:
    """Green tests are not the last word when the implementer rewrote them."""
    repo, origin, log = publishable

    def rewrite_the_tests(cwd: Path) -> None:
        write(cwd, "app.py", APP_TOTAL)
        write(cwd, "test_total.py", TEST_VACUOUS)

    provider = PrdProvider(edits=[rewrite_the_tests])
    result = await run_in_place(repo, home, provider)
    assert result.status == "failed", result.reason
    assert result.reason is not None
    assert "the implementer modified the acceptance tests: test_total.py" in result.reason
    assert result.steps["build[1]/tampered"].status == "succeeded"
    assert (
        "build[1]/typecheck" not in result.steps
        or result.steps["build[1]/typecheck"].status == "skipped"
    )
    assert provider.review_prompts == []
    assert git(repo, "log", "-1", "--format=%s") == TESTS_COMMIT
    assert remote_branches(origin) == ["main"]
    assert not log.exists()


@pytest.mark.anyio
async def test_questions_reach_the_gate_and_the_answer_reaches_the_prompts(
    publishable: tuple[Path, Path, Path], home: Path
) -> None:
    repo, _origin, log = publishable
    provider = PrdProvider(edits=[writes("app.py", APP_TOTAL)], plan=PLAN_WITH_QUESTION)
    answer = "floats too — keep integers exact"
    prompt = Answering(ApprovalAnswer(True, answer))
    result = await run_in_place(repo, home, provider, options=RunOptions(), approval_prompt=prompt)
    assert result.status == "succeeded", result.reason
    # the plan gate showed the question with its assumption; the PR gate approved itself
    (request,) = prompt.requests
    assert request.step_path == "gate_plan"
    assert f'Implement "{TITLE}" (2 requirements, 2 acceptance criteria)?' in request.message
    assert "- R1 — app.total(xs) returns the sum: test_total_sums\n" in request.message
    assert f"- {QUESTION} → assumption: integers only (affects R1)" in request.message
    assert result.steps["gate_plan"].approved is True
    # the answer reached the test writer, the implementer and the reviewer
    (tests,) = provider.test_prompts
    assert f"- {QUESTION} → assumption: integers only (affects R1)\n" in tests
    assert f"Answers given at the plan gate (they override the assumptions):\n{answer}\n" in tests
    (implement,) = provider.implement_prompts
    assert (
        f"Answers given at the plan gate (they override the assumptions):\n{answer}\n" in implement
    )
    (review,) = provider.review_prompts
    assert f"Answers given at the plan gate:\n{answer}\n" in review
    # and both are in the PR body, for whoever reads it after the fact
    logged = log.read_text(encoding="utf-8")
    assert f"## Assumptions\n\n- {QUESTION} → integers only (affects R1)\n" in logged
    assert f"Answers given at the plan gate: {answer}\n" in logged
    assert result.outputs is not None
    assert result.outputs["unresolved"] == PLAN_WITH_QUESTION["unresolved"]


@pytest.mark.anyio
async def test_a_partial_review_asks_at_the_pr_gate_and_still_opens_the_pr(
    publishable: tuple[Path, Path, Path], home: Path
) -> None:
    repo, origin, log = publishable
    provider = PrdProvider(edits=[writes("app.py", APP_TOTAL)], review=PARTIAL)
    prompt = Answering(ApprovalAnswer(True, ""), ApprovalAnswer(True, "ship it, R2 follows"))
    result = await run_in_place(repo, home, provider, options=RunOptions(), approval_prompt=prompt)
    assert result.status == "succeeded", result.reason
    plan_gate, pr_gate = prompt.requests
    assert (plan_gate.step_path, pr_gate.step_path) == ("gate_plan", "gate_pr")
    assert "Reviewer's verdict: partial." in pr_gate.message
    assert "uncovered: R2\nunrequested: logging\n" in pr_gate.message
    assert result.outputs is not None
    assert (result.outputs["verdict"], result.outputs["uncovered"]) == ("partial", ["R2"])
    assert result.outputs["unrequested"] == ["logging"]
    assert remote_branches(origin) == ["main", f"prd/prd-{short_id(result)}"]
    logged = log.read_text(encoding="utf-8")
    assert (
        "## Coverage (reviewer's verdict: partial)\n\ncovered:\n- R1\n\nuncovered:\n- R2\n"
        in logged
    )


@pytest.mark.anyio
async def test_the_scope_gate_pauses_under_a_held_policy_before_any_test_is_written(
    publishable: tuple[Path, Path, Path], home: Path
) -> None:
    repo, origin, _log = publishable
    provider = PrdProvider(edits=[writes("app.py", APP_TOTAL)])
    result = await run_in_place(
        repo,
        home,
        provider,
        options=RunOptions(yes=True, interactive=False, approval_classes=SCOPE_HELD),
    )
    assert result.status == "paused", result.reason
    assert result.exit_code == 3
    assert provider.plan_prompts and provider.test_prompts == []
    assert git(repo, "status", "--porcelain") == ""
    assert remote_branches(origin) == ["main"]
