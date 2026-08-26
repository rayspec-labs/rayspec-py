"""The bundled ``resolve_conflicts`` workflow against a real git merge.

Two nets, both on throw-away repositories whose ``feature`` branch and ``main`` conflict:

* ``resolve_conflicts/checks.yaml`` — declarative cases driven through ``rayspec test --exec-shell``
  with the stub provider: the merge, the conflict listing, the three-stage dump, the marker check
  and the commit run for real; the agents are scripted. One fresh repository per case, because a
  merge mutates it.
* an in-process run with :class:`ResolvingProvider` standing in for ``claude`` — an agent that
  *edits the checkout*, which is the one thing the stub provider cannot do. That is the only way
  to exercise the resolved paths (first try, after failing tests, third try), since the workflow's
  verdict is a property of the tree, not of anything an agent says.

Two fixtures: ``repo`` has one conflicted file; ``messy_repo`` has three — ``app.py``, a file with
a space in its name and a file deleted on one side — so the per-file alignment of the analyst's
verdicts, the gate's listing and the marker scan face what real merges look like.
"""

from __future__ import annotations

import json
import os
import re
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
#: The user's ~/.gitconfig and the system config stay out of the fixture AND out of the
#: workflow's own `git merge`/`git commit` (shell steps inherit the process environment).
GIT_ENV = {
    "GIT_AUTHOR_NAME": "rayspec-test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "rayspec-test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def write(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")


def _init(root: Path) -> None:
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "app.py", "VALUE = 1\n")
    write(root, "README.md", "# demo\n")


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Path:
    """``feature`` (checked out) and ``main`` both changed ``VALUE`` in ``app.py``."""
    root = tmp_path / "repo"
    _init(root)
    commit_all(root, "init")
    git(root, "checkout", "-q", "-b", "feature")
    write(root, "app.py", "VALUE = 2\n")
    commit_all(root, "feature: bump to 2")
    git(root, "checkout", "-q", "main")
    write(root, "app.py", "VALUE = 3\n")
    commit_all(root, "main: bump to 3")
    git(root, "checkout", "-q", "feature")
    return root


@pytest.fixture
def messy_repo(tmp_path: Path, home: Path) -> Path:
    """Three conflicts: ``app.py`` (both modified), ``my file.py`` (both modified, a space in the
    name) and ``gone.py`` (deleted on ``feature``, modified on ``main``)."""
    root = tmp_path / "repo"
    _init(root)
    write(root, "my file.py", "WORDS = 'a'\n")
    write(root, "gone.py", "GONE = 1\n")
    commit_all(root, "init")
    git(root, "checkout", "-q", "-b", "feature")
    write(root, "app.py", "VALUE = 2\n")
    write(root, "my file.py", "WORDS = 'b'\n")
    (root / "gone.py").unlink()
    commit_all(root, "feature: bump to 2, drop gone.py")
    git(root, "checkout", "-q", "main")
    write(root, "app.py", "VALUE = 3\n")
    write(root, "my file.py", "WORDS = 'c'\n")
    write(root, "gone.py", "GONE = 2\n")
    commit_all(root, "main: bump to 3, keep gone.py")
    git(root, "checkout", "-q", "feature")
    return root


def has_markers(repo: Path, name: str = "app.py") -> bool:
    text = (repo / name).read_text(encoding="utf-8")
    return any(marker in text for marker in MARKERS)


def in_merge(repo: Path) -> bool:
    return (repo / ".git" / "MERGE_HEAD").exists()


# --------------------------------------------------------------------------------------------------
# rayspec test --exec-shell over resolve_conflicts/checks.yaml
# --------------------------------------------------------------------------------------------------


def _run_case(root: Path, case_id: str, monkeypatch: pytest.MonkeyPatch) -> str:
    shutil.copy(SUITE / "checks.yaml", root / "checks.yaml")
    shutil.copytree(SUITE / "stubs", root / "stubs")
    shutil.copytree(SUITE / "policy", root / "policy")
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
def test_every_real_git_case_passes(
    request: pytest.FixtureRequest, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `messy_` case runs on the three-conflict repository, every other one on the single one."""
    root = request.getfixturevalue("messy_repo" if case_id.startswith("messy_") else "repo")
    _run_case(root, case_id, monkeypatch)


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
Verdict = dict[str, str]
CONFLICT_LINE = re.compile(r"Conflict \d+ of \d+: `(.+?)`")


def write_value(value: int) -> Resolution:
    """Resolve app.py to ``VALUE = <value>`` and stage it."""

    def resolve(cwd: Path) -> None:
        write(cwd, "app.py", f"VALUE = {value}\n")
        git(cwd, "add", "app.py")

    return resolve


def stage_with_markers(cwd: Path) -> None:
    """`git add` on the still-conflicted file — the index looks merged, the tree is not."""
    git(cwd, "add", "app.py")


def stage_markers_then_fix_the_tree(cwd: Path) -> None:
    """`git add` with the markers in, then fix the file and forget to add it again — the index
    (what gets committed) carries markers while the tree (what the tests see) is clean."""
    git(cwd, "add", "app.py")
    write(cwd, "app.py", "VALUE = 3\n")


def resolve_messy(cwd: Path) -> None:
    """The complete resolution of every conflict in ``messy_repo``."""
    write(cwd, "app.py", "VALUE = 3\n")
    write(cwd, "my file.py", "WORDS = 'c'\n")
    write(cwd, "gone.py", "GONE = 2\n")
    git(cwd, "add", "app.py", "my file.py", "gone.py")


def resolve_messy_but_stage_the_spaced_file_with_markers(cwd: Path) -> None:
    write(cwd, "app.py", "VALUE = 3\n")
    write(cwd, "gone.py", "GONE = 2\n")
    git(cwd, "add", "app.py", "gone.py", "my file.py")  # `my file.py` still carries its markers


class ResolvingProvider:
    """A ``Provider`` for ``claude``: the analyst gets a verdict per file, the resolver edits files.

    ``verdicts`` maps a conflicted path to its verdict (``"*"`` is the fallback); the file is read
    off the analyst prompt. Every resolver call pops the next scripted resolution and applies it
    to the run's working directory; both kinds of prompt are recorded.
    """

    id = "claude"
    capabilities = STUB_CAPABILITIES

    def __init__(self, verdicts: Mapping[str, Verdict], resolutions: list[Resolution]) -> None:
        self.verdicts = dict(verdicts)
        self.resolutions = list(resolutions)
        self.prompts: list[str] = []
        self.analyst_prompts: list[str] = []

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
            self.analyst_prompts.append(req.prompt)
            match = CONFLICT_LINE.search(req.prompt)
            assert match is not None, req.prompt
            verdict = self.verdicts.get(match.group(1), self.verdicts.get("*"))
            assert verdict is not None, match.group(1)
            return AgentResult(status="success", text=json.dumps(verdict), structured=dict(verdict))
        self.prompts.append(req.prompt)
        self.resolutions.pop(0)(Path(req.cwd))
        return AgentResult(status="success", text="Resolved and staged.")


MECHANICAL: Verdict = {"strategy": "theirs", "risk": "low", "reason": "main bumped VALUE"}
RISKY: Verdict = {"strategy": "manual", "risk": "high", "reason": "both sides changed VALUE"}
# -B: no __pycache__ in the checkout — a stale .pyc (same size, same mtime second) would
# otherwise hand a later attempt the previous attempt's VALUE
TEST_COMMAND = "python3 -B -c 'import app; assert app.VALUE == 3, app.VALUE'"
#: what the workflow's header asks operators for: `risky` is never approved automatically
RISKY_HELD = ApprovalClasses(rules={"risky": ClassRules(allow_yes=False)}, policy_loaded=True)


async def run_in_place(
    repo: Path,
    home: Path,
    provider: ResolvingProvider,
    *,
    options: RunOptions | None = None,
    **inputs: Any,
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
        options=options,
        handle_signals=False,
    ).run()


def assert_merged(repo: Path) -> None:
    assert not in_merge(repo)
    assert not has_markers(repo)
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert git(repo, "status", "--porcelain") == ""
    parents = git(repo, "rev-list", "--parents", "-1", "HEAD").split()
    assert len(parents) == 3, "HEAD is not a merge commit"
    committed = git(repo, "grep", "-l", "-E", "^(<{7}|>{7})( |$)", "HEAD", "--") if False else ""
    assert committed == ""


def committed_files_with_markers(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "grep", "-l", "-E", "^(<{7}|>{7})( |$)", "HEAD", "--"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.split(":", 1)[1] for line in proc.stdout.splitlines() if ":" in line]


@pytest.mark.anyio
async def test_a_mechanical_conflict_is_resolved_and_committed_in_one_attempt(
    repo: Path, home: Path
) -> None:
    provider = ResolvingProvider({"*": MECHANICAL}, [write_value(3)])
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
    provider = ResolvingProvider({"*": MECHANICAL}, [write_value(2), write_value(3)])
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
    provider = ResolvingProvider(
        {"*": MECHANICAL}, [stage_with_markers, write_value(2), write_value(3)]
    )
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None and result.outputs["attempts"] == 3
    _, second, third = provider.prompts
    assert "conflict markers left behind" in second
    assert "Attempt 3 of 3" in third and "AssertionError: 2" in third
    assert_merged(repo)


@pytest.mark.anyio
async def test_max_attempts_bounds_the_loop_and_nothing_is_committed(
    repo: Path, home: Path
) -> None:
    provider = ResolvingProvider({"*": MECHANICAL}, [stage_with_markers, write_value(3)])
    result = await run_in_place(repo, home, provider, max_attempts=1)
    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.reason is not None
    assert "not resolved after 1 attempt(s)" in result.reason
    assert "conflict markers left behind" in result.reason
    assert len(provider.prompts) == 1
    assert in_merge(repo)
    assert git(repo, "log", "-1", "--format=%s") == "feature: bump to 2"


@pytest.mark.anyio
async def test_a_marker_left_in_the_index_never_reaches_the_commit(repo: Path, home: Path) -> None:
    """The tree is clean and the tests pass, but the index still holds the markers."""
    provider = ResolvingProvider(
        {"*": MECHANICAL}, [stage_markers_then_fix_the_tree, write_value(3)]
    )
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None and result.outputs["attempts"] == 2
    assert "conflict markers left behind" in provider.prompts[1]
    assert committed_files_with_markers(repo) == []
    assert_merged(repo)


@pytest.mark.anyio
async def test_a_marker_in_a_file_with_a_space_in_its_name_is_caught(
    messy_repo: Path, home: Path
) -> None:
    provider = ResolvingProvider(
        {"*": MECHANICAL},
        [resolve_messy_but_stage_the_spaced_file_with_markers, resolve_messy],
    )
    result = await run_in_place(messy_repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None and result.outputs["attempts"] == 2
    assert "conflict markers left behind in:\nmy file.py" in provider.prompts[1]
    assert committed_files_with_markers(messy_repo) == []
    assert_merged(messy_repo)


@pytest.mark.anyio
async def test_three_conflicts_including_a_deletion_and_a_space_are_resolved(
    messy_repo: Path, home: Path
) -> None:
    provider = ResolvingProvider({"*": MECHANICAL}, [resolve_messy])
    result = await run_in_place(messy_repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.outputs is not None
    assert result.outputs["files"] == ["app.py", "gone.py", "my file.py"]
    assert result.outputs["attempts"] == 1 and result.outputs["risky"] == 0
    # the analyst saw each file's three stages, including the side that deleted one
    (gone,) = [p for p in provider.analyst_prompts if "`gone.py`" in p]
    assert "Conflict 2 of 3: `gone.py`" in gone
    assert "=== OURS (HEAD) ===\n(deleted on our side)\n=== THEIRS (main) ===\nGONE = 2\n" in gone
    # the resolver's brief lists every file with ITS verdict, in the order of the conflict list
    (brief,) = provider.prompts
    lines = [line for line in brief.splitlines() if line.startswith("- `")]
    assert lines == [
        "- `app.py` → theirs (low risk): main bumped VALUE",
        "- `gone.py` → theirs (low risk): main bumped VALUE",
        "- `my file.py` → theirs (low risk): main bumped VALUE",
    ]
    assert_merged(messy_repo)
    assert (messy_repo / "gone.py").read_text(encoding="utf-8") == "GONE = 2\n"
    assert (messy_repo / "my file.py").read_text(encoding="utf-8") == "WORDS = 'c'\n"


@pytest.mark.anyio
async def test_the_gate_lists_only_the_risky_files_and_touches_nothing(
    messy_repo: Path, home: Path
) -> None:
    provider = ResolvingProvider({"app.py": RISKY, "*": MECHANICAL}, [])
    result = await run_in_place(
        messy_repo,
        home,
        provider,
        options=RunOptions(interactive=False, approval_classes=RISKY_HELD),
    )
    assert result.status == "paused", result.reason
    assert result.exit_code == 3
    assert result.pause is not None
    message = result.pause.message
    assert (
        "1 of 3 conflict(s) need judgement:\n- app.py → manual: both sides changed VALUE\n"
        in message
    )
    assert "my file.py" not in message and "gone.py" not in message
    assert message.rstrip().endswith("Let the resolver touch them?")
    assert provider.prompts == []  # the resolver was never asked
    assert len(provider.analyst_prompts) == 3
    assert in_merge(messy_repo) and has_markers(messy_repo)
    assert git(messy_repo, "log", "-1", "--format=%s") == "feature: bump to 2, drop gone.py"
