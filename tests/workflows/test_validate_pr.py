"""The bundled ``validate_pr`` workflow against real test runs on two refs.

Two nets, each on a throw-away repository whose checked-out ``feature`` branch plays the pull
request (``main`` is the base):

* ``validate_pr/checks.yaml`` — declarative cases driven through ``rayspec test --exec-shell``
  with the stub provider: one fixture per classification (clean, regression, preexisting, fixed,
  mixed), the tests really run on both sides, `compare` is checked against what they printed, and
  a fake ``gh`` first on ``PATH`` checks the "PR" out as a detached HEAD (or fails on request).
  After every case the checkout must be back on ``feature`` — the `restore` step's job.
* an in-process run with :class:`JudgeProvider` standing in for ``claude`` and *real* pytest as the
  test command, so the default ``failure_pattern`` is proven against pytest's own short summary,
  not the fixture runner's imitation of it.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.config import Config
from rayspec.engine.context import RunOptions
from rayspec.engine.runner import Runner, RunResult, Workspace
from rayspec.loader import load_workflow
from rayspec.loader.inputs import resolve_inputs
from rayspec.providers.base import AgentRequest, AgentResult, EmitFn, ProviderHealth
from rayspec.providers.stub import STUB_CAPABILITIES
from rayspec.store.file import FileRunStore
from rayspec.testing import load_checks

HERE = Path(__file__).resolve().parent
SUITE = HERE / "validate_pr"
CASES = load_checks(SUITE / "checks.yaml")
runner = CliRunner()

#: The user's ~/.gitconfig and the system config stay out of the fixture and out of the workflow's
#: own git commands (shell steps inherit the process environment).
GIT_ENV = {
    "GIT_AUTHOR_NAME": "rayspec-test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "rayspec-test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

FAKE_GH = """#!/bin/sh
# Records every invocation in $GH_LOG; `pr checkout` detaches onto the branch that plays the PR
# (like `gh pr checkout --detach` would onto the PR head), `pr diff` prints the branch's diff;
# fails the subcommand named by $GH_FAIL the way a real `gh` would (non-zero exit, stderr line).
printf '%s\\n' "gh $*" >> "$GH_LOG"
if [ -n "${GH_FAIL:-}" ] && [ "$2" = "$GH_FAIL" ]; then
  echo "gh: simulated $GH_FAIL failure" >&2
  exit 1
fi
case "$1 $2" in
  "pr checkout") git checkout -q --detach "${GH_PR_HEAD:-feature}" ;;
  "pr diff") git diff main...HEAD ;;
esac
exit 0
"""

#: test_app.py: three checks on app.py's constants, reported the way pytest's short summary would.
RUNNER = """\
import sys

import app

failed = []
for name, attr, want in (("test_a", "A", 1), ("test_b", "B", 2), ("test_c", "C", 3)):
    got = getattr(app, attr)
    if got != want:
        failed.append(name)
        print(f"FAILED test_app.py::{name} - AssertionError: {attr} == {got}")
print(f"{len(failed)} failed, {3 - len(failed)} passed")
sys.exit(1 if failed else 0)
"""

PYTEST_FILE = """\
import app


def test_a():
    assert app.A == 1


def test_b():
    assert app.B == 2


def test_c():
    assert app.C == 3
"""

Values = dict[str, int]
GREEN: Values = {"A": 1, "B": 2, "C": 3}
B_RED: Values = {"A": 1, "B": 0, "C": 3}
C_RED: Values = {"A": 1, "B": 2, "C": 0}
#: case id → (app.py on main, app.py on feature); feature always adds a comment so the PR has a diff
SCENARIOS: dict[str, tuple[Values, Values]] = {
    "clean": (GREEN, GREEN),
    "regression": (GREEN, B_RED),
    "preexisting": (B_RED, B_RED),
    "fixed": (B_RED, GREEN),
    "mixed": (B_RED, C_RED),
    "checkout_fails": (GREEN, GREEN),
}


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def app_py(values: Values) -> str:
    return "".join(f"{name} = {value}\n" for name, value in values.items())


def make_repo(root: Path, main: Values, feature: Values, *, tests: str = RUNNER) -> Path:
    """``main`` holds app.py + test_app.py; ``feature`` (checked out) changes app.py."""
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text(app_py(main), encoding="utf-8")
    (root / "test_app.py").write_text(tests, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "app.py").write_text(app_py(feature) + "# feature\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "feature")
    return root


# --------------------------------------------------------------------------------------------------
# rayspec test --exec-shell over validate_pr/checks.yaml, with a fake gh on PATH
# --------------------------------------------------------------------------------------------------


def install_fake_gh(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``gh`` that detaches onto ``feature`` instead of talking to GitHub; returns the log."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(FAKE_GH, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = root / "gh.log"
    # a case's `env:` is a literal string and cannot name this directory: the driver sets both
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(log))
    monkeypatch.delenv("GH_FAIL", raising=False)
    return log


def _run_case(root: Path, case_id: str, monkeypatch: pytest.MonkeyPatch) -> str:
    shutil.copy(SUITE / "checks.yaml", root / "checks.yaml")
    shutil.copytree(SUITE / "stubs", root / "stubs")
    log = install_fake_gh(root, monkeypatch)
    monkeypatch.chdir(root)
    res = runner.invoke(app, ["test", "--root", str(root), "--exec-shell", "--case", case_id])
    assert res.exit_code == 0, res.output
    assert f"ok checks:{case_id}" in res.output, res.output
    # `restore` — whatever happened, the checkout is back where it started
    assert git(root, "symbolic-ref", "--short", "HEAD") == "feature"
    return log.read_text(encoding="utf-8")


@pytest.mark.parametrize("case_id", [case.id for case in CASES])
def test_every_classification_case_passes(
    tmp_path: Path, home: Path, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, feature = SCENARIOS[case_id]
    repo = make_repo(tmp_path / "repo", main, feature)
    log = _run_case(repo, case_id, monkeypatch)
    assert log.startswith("gh pr checkout 7 --detach\n")
    if case_id == "checkout_fails":
        assert log == "gh pr checkout 7 --detach\n"  # no diff was asked for
    else:
        assert log == "gh pr checkout 7 --detach\ngh pr diff 7\n"


# --------------------------------------------------------------------------------------------------
# in-process: the default failure_pattern against pytest's own short summary
# --------------------------------------------------------------------------------------------------


class JudgeProvider:
    """A ``Provider`` for ``claude`` that records the judge's brief and answers with a script."""

    id = "claude"
    capabilities = STUB_CAPABILITIES

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def open(
        self, *, run_id: str, workdir: str, env: Mapping[str, str], max_parallel: int
    ) -> None:
        return None

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
        return ProviderHealth(ok=True, sdk_version=None, auth="ok", details=())

    async def aclose(self) -> None:
        return None

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        self.prompts.append(req.prompt)
        answer: dict[str, Any] = {"summary": "interpreted", "recommendation": "fix_pr"}
        return AgentResult(status="success", text=json.dumps(answer), structured=answer)


async def run_in_place(repo: Path, home: Path, provider: JudgeProvider, **inputs: Any) -> RunResult:
    resolved = load_workflow("validate_pr", project_root=repo, home=home, config=Config())
    return await Runner(
        resolved,
        # what the CLI does before it builds a Runner: defaults filled in, values coerced
        inputs=resolve_inputs(
            resolved.workflow, cli_pairs=[f"{name}={value}" for name, value in inputs.items()]
        ),
        store=FileRunStore(home / "store"),
        project_root=repo,
        project_slug="local/test",
        workspace=Workspace.in_place(repo),
        providers={"claude": provider},
        options=RunOptions(),
        handle_signals=False,
    ).run()


@pytest.mark.anyio
async def test_the_default_pattern_parses_real_pytest_output(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With pytest itself as the test command, the default `failure_pattern` names the regression
    from pytest's `FAILED <nodeid> - …` short-summary line, and the judge is shown it."""
    repo = make_repo(tmp_path / "repo", GREEN, B_RED, tests=PYTEST_FILE)
    install_fake_gh(repo, monkeypatch)
    provider = JudgeProvider()
    result = await run_in_place(
        repo,
        home,
        provider,
        pr=7,
        test_command=f"{sys.executable} -B -m pytest -q -p no:cacheprovider test_app.py",
    )
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None
    assert result.outputs["status"] == "regression"
    assert result.outputs["newly_failing"] == ["test_app.py::test_b"]
    assert result.outputs["newly_passing"] == []
    assert (result.outputs["base_exit"], result.outputs["head_exit"]) == (0, 1)
    (brief,) = provider.prompts
    assert '"newly_failing": [\n    "test_app.py::test_b"\n  ]' in brief
    assert "FAILED test_app.py::test_b - assert 0 == 2" in brief
    assert "-B = 2\n+B = 0\n" in brief
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "feature"
