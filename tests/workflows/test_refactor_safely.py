"""The bundled ``refactor_safely`` workflow against a real tree.

Two nets, both on a throw-away repository with ``app.py``, a test runner and a syntax check that
stands in for a typechecker:

* ``refactor_safely/checks.yaml`` — declarative cases driven through ``rayspec test --exec-shell``
  with the stub provider, which never edits a file: a red baseline stops the run before the
  planner, an untouched tree ends in "nothing to review", a held ``risky`` class pauses at the gate.
* in-process runs with :class:`RefactoringProvider` standing in for ``claude`` — an agent that
  really edits ``app.py``, which is the only way to see the loop converge, the "does not
  typecheck" and "typechecks, but the tests fail" briefs, the fresh reviewer flagging a behaviour
  change the green tests missed, and the give-up bound.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.config import Config
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
SUITE = HERE / "refactor_safely"
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
def helper(xs):
    return list(xs)


def total(xs):
    return sum(helper(xs))
"""
#: the refactor the tests ask for: the same behaviour under a new name
APP_RENAMED = """\
def items(xs):
    return list(xs)


def total(xs):
    return sum(items(xs))
"""
APP_BROKEN_SYNTAX = "def total(xs) return sum(xs)\n"
APP_WRONG = APP.replace("return sum(helper(xs))", "return sum(helper(xs)) + 1")
#: a behaviour change the tests do not see: the order of the items
APP_SORTED = APP.replace("return list(xs)", "return sorted(xs)")

#: prints pytest's short-summary shape on failure; exit 1
RUNNER = """\
import sys

import app

try:
    assert app.total([1, 2]) == 3, app.total([1, 2])
    assert app.total([]) == 0, app.total([])
except AssertionError as exc:
    print(f"FAILED test_app.py::test_total - AssertionError: {exc}")
    sys.exit(1)
print("1 passed")
"""
RED_RUNNER = RUNNER.replace("== 3, app.total([1, 2])", "== 4, app.total([1, 2])")
#: a syntax check standing in for a typechecker (nothing is imported, no .pyc is written)
TYPECHECK = """\
import ast
import sys

try:
    ast.parse(open("app.py", encoding="utf-8").read(), "app.py")
except SyntaxError as exc:
    print(f"app.py:{exc.lineno}: SyntaxError: {exc.msg}")
    sys.exit(1)
print("typecheck ok")
"""

GOAL = "rename helper() to items()"
TYPECHECK_COMMAND = "python3 -B typecheck.py"
TEST_COMMAND = "python3 -B test_app.py"
PLAN: dict[str, Any] = {"changes": [{"what": "rename helper", "files": ["app.py"]}], "risks": []}
SHAPE_ONLY: dict[str, Any] = {
    "verdict": "shape_only",
    "reasoning": "a rename; every call site follows",
    "concerns": [],
}
BEHAVIOUR: dict[str, Any] = {
    "verdict": "behaviour_changed",
    "reasoning": "helper() now sorts, so total() sees the items in a different order",
    "concerns": [{"file": "app.py", "note": "helper() now sorts"}],
}
#: what the workflow's header asks operators for: `risky` is never approved automatically
RISKY_HELD = ApprovalClasses(rules={"risky": ClassRules(allow_yes=False)}, policy_loaded=True)


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")


def make_repo(root: Path, *, tests: str = RUNNER) -> Path:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "app.py", APP)
    write(root, "test_app.py", tests)
    write(root, "typecheck.py", TYPECHECK)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def red_repo(tmp_path: Path, home: Path) -> Path:
    """The same tree with a test that fails before anything is touched."""
    return make_repo(tmp_path / "red", tests=RED_RUNNER)


# --------------------------------------------------------------------------------------------------
# rayspec test --exec-shell over refactor_safely/checks.yaml
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


@pytest.mark.parametrize("case_id", [case.id for case in CASES])
def test_every_real_tree_case_passes(
    request: pytest.FixtureRequest, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = request.getfixturevalue("red_repo" if case_id.startswith("red_") else "repo")
    _run_case(root, case_id, monkeypatch)
    # none of these cases edits or commits anything
    assert git(root, "log", "-1", "--format=%s") == "suite"
    assert git(root, "status", "--porcelain") == ""


# --------------------------------------------------------------------------------------------------
# in-process: an agent that really edits files
# --------------------------------------------------------------------------------------------------

Edit = Callable[[Path], None]


def writes(text: str) -> Edit:
    def edit(cwd: Path) -> None:
        write(cwd, "app.py", text)

    return edit


class RefactoringProvider:
    """A ``Provider`` for ``claude``: the planner returns ``plan``, every refactorer call pops the
    next scripted edit and applies it to the run's working directory, the reviewer returns
    ``verdict`` (``None`` = a provider outage). Every prompt is recorded by role — the schema a
    request carries says which agent is asking."""

    id = "claude"
    capabilities = STUB_CAPABILITIES

    def __init__(
        self, plan: dict[str, Any], edits: list[Edit], verdict: dict[str, Any] | None
    ) -> None:
        self.plan = plan
        self.edits = list(edits)
        self.verdict = verdict
        self.plan_prompts: list[str] = []
        self.edit_prompts: list[str] = []
        self.review_prompts: list[str] = []

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
        if "changes" in properties:  # the planner
            self.plan_prompts.append(req.prompt)
            return AgentResult(status="success", text=json.dumps(self.plan), structured=self.plan)
        if "verdict" in properties:  # the reviewer
            self.review_prompts.append(req.prompt)
            if self.verdict is None:
                error = AgentError(kind="api", message="simulated outage", transient=False)
                return AgentResult(status="error", text="", error=error)
            return AgentResult(
                status="success", text=json.dumps(self.verdict), structured=self.verdict
            )
        self.edit_prompts.append(req.prompt)
        self.edits.pop(0)(Path(req.cwd))
        return AgentResult(status="success", text="Edited app.py; checks run.")


async def run_in_place(
    repo: Path,
    home: Path,
    provider: RefactoringProvider,
    *,
    options: RunOptions | None = None,
    **inputs: Any,
) -> RunResult:
    resolved = load_workflow("refactor_safely", project_root=repo, home=home, config=Config())
    given = {
        "goal": GOAL,
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
        # a real run: the gate needs an answer — `yes`, which a held class still refuses
        options=options or RunOptions(yes=True),
        handle_signals=False,
    ).run()


def staged_changes(repo: Path) -> bool:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=repo, check=False, capture_output=True
    )
    return proc.returncode == 1


@pytest.mark.anyio
async def test_a_shape_only_refactor_converges_and_is_committed(repo: Path, home: Path) -> None:
    provider = RefactoringProvider(PLAN, [writes(APP_RENAMED)], SHAPE_ONLY)
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None
    assert (result.outputs["attempts"], result.outputs["verdict"]) == (1, "shape_only")
    assert result.outputs["commit"].endswith(f" refactor: {GOAL}")
    assert git(repo, "log", "-1", "--format=%s") == f"refactor: {GOAL}"
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "app.py").read_text(encoding="utf-8") == APP_RENAMED
    (first,) = provider.edit_prompts
    assert f"Carry out this refactor in {repo}" in first or "Carry out this refactor in " in first
    assert "The plan:\n1. rename helper (app.py)\n" in first
    (review,) = provider.review_prompts
    assert "-def helper(xs):\n+def items(xs):\n" in review
    assert "=== FULL DIFF ===" in review


@pytest.mark.anyio
async def test_a_syntax_error_is_reported_as_not_typechecking(repo: Path, home: Path) -> None:
    provider = RefactoringProvider(
        PLAN, [writes(APP_BROKEN_SYNTAX), writes(APP_RENAMED)], SHAPE_ONLY
    )
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None and result.outputs["attempts"] == 2
    _, second = provider.edit_prompts
    assert second.startswith("Attempt 2 of 3.\n")
    assert (
        "The tree does not typecheck after your last edit (`python3 -B typecheck.py` exited 1):\n"
        "app.py:1: SyntaxError:"
    ) in second
    assert "The tests fail too (`python3 -B test_app.py` exited 1):\n" in second
    assert "Fix it without changing behaviour; do not commit." in second


@pytest.mark.anyio
async def test_green_typecheck_with_red_tests_is_reported_as_such(repo: Path, home: Path) -> None:
    provider = RefactoringProvider(PLAN, [writes(APP_WRONG), writes(APP_RENAMED)], SHAPE_ONLY)
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None and result.outputs["attempts"] == 2
    _, second = provider.edit_prompts
    assert "does not typecheck" not in second
    assert (
        "The tree typechecks, but the tests fail (`python3 -B test_app.py` exited 1):\n"
        "FAILED test_app.py::test_total - AssertionError: 4\n"
    ) in second


@pytest.mark.anyio
async def test_a_behaviour_change_the_tests_miss_is_flagged_and_not_committed(
    repo: Path, home: Path
) -> None:
    """Green tests are not the last word: the fresh reviewer reads the diff."""
    provider = RefactoringProvider(PLAN, [writes(APP_SORTED)], BEHAVIOUR)
    result = await run_in_place(repo, home, provider)
    assert result.status == "failed", result.reason
    assert result.exit_code == 1
    assert result.reason is not None
    assert "the reviewer says behaviour changed — nothing committed" in result.reason
    assert "- app.py: helper() now sorts" in result.reason
    assert git(repo, "log", "-1", "--format=%s") == "init"
    assert staged_changes(repo)  # left for the human to inspect
    (review,) = provider.review_prompts
    assert "-    return list(xs)\n+    return sorted(xs)\n" in review


@pytest.mark.anyio
async def test_gives_up_after_max_attempts(repo: Path, home: Path) -> None:
    provider = RefactoringProvider(PLAN, [writes(APP_WRONG), writes(APP_WRONG)], SHAPE_ONLY)
    result = await run_in_place(repo, home, provider, max_attempts=2)
    assert result.status == "failed", result.reason
    assert result.reason is not None
    assert "not green after 2 attempt(s) — typecheck exited 0, tests exited 1" in result.reason
    assert result.steps["work[2]/give_up"].status == "succeeded"
    assert len(provider.edit_prompts) == 2
    assert provider.review_prompts == []  # nothing to review: the loop never went green
    assert git(repo, "log", "-1", "--format=%s") == "init"


@pytest.mark.anyio
async def test_the_gate_pauses_under_a_held_policy_before_any_edit(repo: Path, home: Path) -> None:
    provider = RefactoringProvider(PLAN, [writes(APP_RENAMED)], SHAPE_ONLY)
    result = await run_in_place(
        repo,
        home,
        provider,
        options=RunOptions(yes=True, interactive=False, approval_classes=RISKY_HELD),
    )
    assert result.status == "paused", result.reason
    assert result.exit_code == 3
    assert provider.plan_prompts and provider.edit_prompts == []
    assert git(repo, "status", "--porcelain") == ""
