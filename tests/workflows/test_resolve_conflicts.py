"""The bundled ``resolve_conflicts`` workflow against a real git merge.

Two nets, both on a throw-away repository whose ``feature`` branch and ``main`` edit the same line
of ``app.py``:

* ``resolve_conflicts/checks.yaml`` — declarative cases driven through ``rayspec test --exec-shell``
  with the stub provider: the merge, the conflict listing, the three-stage dump, the marker check
  and the commit run for real; the agents are scripted. One fresh repository per case, because a
  merge mutates it.
* an in-process run with :class:`ResolvingProvider` standing in for ``claude`` — an agent that
  *edits the checkout*, which is the one thing the stub provider cannot do. That is the only way
  to exercise the resolved paths (first try, after failing tests, third try), since the workflow's
  verdict is a property of the tree, not of anything an agent says.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.config import Config
from rayspec.engine.runner import Runner, RunResult, Workspace
from rayspec.loader import load_workflow
from rayspec.loader.inputs import resolve_inputs
from rayspec.providers.base import (
    AgentRequest,
    AgentResult,
    EmitFn,
    ProviderHealth,
)
from rayspec.providers.stub import STUB_CAPABILITIES
from rayspec.store.file import FileRunStore
from rayspec.testing import load_checks

HERE = Path(__file__).resolve().parent
SUITE = HERE / "resolve_conflicts"
CASES = load_checks(SUITE / "checks.yaml")
runner = CliRunner()

MARKERS = ("<<<<<<< ", ">>>>>>> ")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Path:
    """``feature`` (checked out) and ``main`` both changed ``VALUE`` in ``app.py``."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    commit_all(root, "init")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    commit_all(root, "feature: bump to 2")
    git(root, "checkout", "-q", "main")
    (root / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    commit_all(root, "main: bump to 3")
    git(root, "checkout", "-q", "feature")
    return root


def has_markers(repo: Path) -> bool:
    text = (repo / "app.py").read_text(encoding="utf-8")
    return any(marker in text for marker in MARKERS)


def in_merge(repo: Path) -> bool:
    return (repo / ".git" / "MERGE_HEAD").exists()


# --------------------------------------------------------------------------------------------------
# rayspec test --exec-shell over resolve_conflicts/checks.yaml
# --------------------------------------------------------------------------------------------------


def _run_case(repo: Path, case_id: str, monkeypatch: pytest.MonkeyPatch) -> str:
    shutil.copy(SUITE / "checks.yaml", repo / "checks.yaml")
    shutil.copytree(SUITE / "stubs", repo / "stubs")
    shutil.copytree(SUITE / "policy", repo / "policy")
    monkeypatch.chdir(repo)  # a case's RAYSPEC_POLICY is relative to the cwd
    # `rayspec test` reads the operator policy once per suite root, at start-up — before a case's
    # own `env:` is applied — so the policy a case names has to be in force before the command runs.
    (case,) = [c for c in CASES if c.id == case_id]
    if policy := case.env.get("RAYSPEC_POLICY"):
        monkeypatch.setenv("RAYSPEC_POLICY", policy)
    res = runner.invoke(app, ["test", "--root", str(repo), "--exec-shell", "--case", case_id])
    assert res.exit_code == 0, res.output
    assert f"ok checks:{case_id}" in res.output, res.output
    return res.output


@pytest.mark.parametrize("case_id", [case.id for case in CASES])
def test_every_real_git_case_passes(repo: Path, case_id: str, monkeypatch: pytest.MonkeyPatch):
    _run_case(repo, case_id, monkeypatch)


def test_giving_up_leaves_the_merge_for_a_human(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Exhausted → no commit, the merge is still in progress and the markers are still there."""
    _run_case(repo, "leaves_markers", monkeypatch)
    assert git(repo, "log", "-1", "--format=%s") == "feature: bump to 2"
    assert in_merge(repo)
    assert has_markers(repo)


def test_a_held_gate_modifies_nothing(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Paused at `risky` → analysis happened, resolution did not."""
    _run_case(repo, "risky_pauses", monkeypatch)
    assert git(repo, "log", "-1", "--format=%s") == "feature: bump to 2"
    assert in_merge(repo)
    assert has_markers(repo)


# --------------------------------------------------------------------------------------------------
# in-process: an agent that edits the checkout
# --------------------------------------------------------------------------------------------------

Resolution = Callable[[Path], None]


def write_value(value: int) -> Resolution:
    """Resolve app.py to ``VALUE = <value>`` and stage it."""

    def resolve(cwd: Path) -> None:
        (cwd / "app.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
        git(cwd, "add", "app.py")

    return resolve


def stage_with_markers(cwd: Path) -> None:
    """`git add` on the still-conflicted file — the index looks merged, the tree is not."""
    git(cwd, "add", "app.py")


class ResolvingProvider:
    """A ``Provider`` for ``claude``: the analyst gets a fixed verdict, the resolver edits files."""

    id = "claude"
    capabilities = STUB_CAPABILITIES

    def __init__(self, verdict: Mapping[str, Any], resolutions: list[Resolution]) -> None:
        self.verdict = dict(verdict)
        self.resolutions = list(resolutions)
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
        if req.output_schema is not None:  # the analyst
            return AgentResult(
                status="success", text=json.dumps(self.verdict), structured=dict(self.verdict)
            )
        self.prompts.append(req.prompt)
        self.resolutions.pop(0)(Path(req.cwd))
        return AgentResult(status="success", text="Resolved and staged.")


MECHANICAL = {"strategy": "theirs", "risk": "low", "reason": "main bumped VALUE"}
# -B: no __pycache__ in the checkout — a stale .pyc (same size, same mtime second) would
# otherwise hand a later attempt the previous attempt's VALUE
TEST_COMMAND = "python3 -B -c 'import app; assert app.VALUE == 3, app.VALUE'"


async def run_in_place(
    repo: Path, home: Path, provider: ResolvingProvider, **inputs: Any
) -> RunResult:
    resolved = load_workflow("resolve_conflicts", project_root=repo, home=home, config=Config())
    given = {"test_command": TEST_COMMAND, **inputs}
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
        handle_signals=False,
    ).run()


def assert_merged(repo: Path) -> None:
    assert not in_merge(repo)
    assert not has_markers(repo)
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert git(repo, "status", "--porcelain") == ""
    parents = git(repo, "rev-list", "--parents", "-1", "HEAD").split()
    assert len(parents) == 3, "HEAD is not a merge commit"


@pytest.mark.anyio
async def test_a_mechanical_conflict_is_resolved_and_committed_in_one_attempt(
    repo: Path, home: Path
) -> None:
    provider = ResolvingProvider(MECHANICAL, [write_value(3)])
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.exit_code == 0
    assert result.outputs is not None
    assert result.outputs["files"] == ["app.py"]
    assert result.outputs["risky"] == 0
    assert result.outputs["attempts"] == 1
    assert "Merge branch 'main' into feature" in result.outputs["commit"]
    assert_merged(repo)


@pytest.mark.anyio
async def test_failing_tests_are_retried_with_the_failure_in_the_next_prompt(
    repo: Path, home: Path
) -> None:
    provider = ResolvingProvider(MECHANICAL, [write_value(2), write_value(3)])
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None and result.outputs["attempts"] == 2
    first, second = provider.prompts
    assert "Attempt" not in first
    assert "Attempt 2 of 3" in second
    assert "AssertionError: 2" in second  # the test command's stderr reaches the agent
    assert_merged(repo)


@pytest.mark.anyio
async def test_resolved_on_the_third_try(repo: Path, home: Path) -> None:
    provider = ResolvingProvider(MECHANICAL, [stage_with_markers, write_value(2), write_value(3)])
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None and result.outputs["attempts"] == 3
    _, second, third = provider.prompts
    assert "conflict markers left in a staged file" in second
    assert "Attempt 3 of 3" in third and "AssertionError: 2" in third
    assert_merged(repo)


@pytest.mark.anyio
async def test_max_attempts_bounds_the_loop_and_nothing_is_committed(
    repo: Path, home: Path
) -> None:
    provider = ResolvingProvider(MECHANICAL, [stage_with_markers, write_value(3)])
    result = await run_in_place(repo, home, provider, max_attempts=1)
    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.reason is not None
    assert "not resolved after 1 attempt(s)" in result.reason
    assert "conflict markers left in a staged file" in result.reason
    assert len(provider.prompts) == 1
    assert in_merge(repo)
    assert git(repo, "log", "-1", "--format=%s") == "feature: bump to 2"
