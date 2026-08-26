"""The bundled ``review_panel`` workflow against a real diff.

Two nets, both on a throw-away repository whose checked-out ``feature`` branch differs from ``main``:

* ``review_panel/checks.yaml`` — declarative cases driven through ``rayspec test --exec-shell`` with
  the stub provider: the diff is taken for real, the reviewers are pinned to have seen it, and a fake
  ``gh`` first on ``PATH`` records the checkout and the comment (or fails the checkout on request).
* in-process runs with :class:`PanelProvider` standing in for ``claude`` — the one way to observe
  what the stub cannot: that the lenses really run *concurrently*, and that a resumed run replays
  the panel from cache instead of asking the reviewers again.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.config import Config
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
SUITE = HERE / "review_panel"
CASES = load_checks(SUITE / "checks.yaml")
runner = CliRunner()

LENSES = ["security", "correctness", "performance", "api_design", "tests"]
#: The user's ~/.gitconfig and the system config stay out of the fixture and out of the workflow's
#: own `git diff` (shell steps inherit the process environment).
GIT_ENV = {
    "GIT_AUTHOR_NAME": "rayspec-test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "rayspec-test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

FAKE_GH = """#!/bin/sh
# Records every invocation (and the body of `pr comment`) in $GH_LOG; fails the subcommand named
# by $GH_FAIL the way a real `gh` would (non-zero exit, a line on stderr).
printf '%s\\n' "gh $*" >> "$GH_LOG"
if [ -n "${GH_FAIL:-}" ] && [ "$2" = "$GH_FAIL" ]; then
  echo "gh: simulated $GH_FAIL failure" >&2
  exit 1
fi
if [ "$1 $2" = "pr comment" ]; then
  cat >> "$GH_LOG"
  echo "--- end of body" >> "$GH_LOG"
fi
exit 0
"""


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


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Path:
    """``feature`` (checked out) bumps ``VALUE`` and adds ``helper()``; ``main`` has neither."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "app.py", "VALUE = 1\n")
    write(root, "README.md", "# demo\n")
    commit_all(root, "init")
    git(root, "checkout", "-q", "-b", "feature")
    write(root, "app.py", "VALUE = 2\n\n\ndef helper():\n    return VALUE\n")
    commit_all(root, "feature: bump VALUE, add helper")
    return root


# --------------------------------------------------------------------------------------------------
# rayspec test --exec-shell over review_panel/checks.yaml, with a fake gh on PATH
# --------------------------------------------------------------------------------------------------


def install_fake_gh(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``gh`` that logs instead of talking to GitHub; returns the log file."""
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


def _run_case(root: Path, case_id: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copy(SUITE / "checks.yaml", root / "checks.yaml")
    shutil.copytree(SUITE / "stubs", root / "stubs")
    log = install_fake_gh(root, monkeypatch)
    monkeypatch.chdir(root)
    res = runner.invoke(app, ["test", "--root", str(root), "--exec-shell", "--case", case_id])
    assert res.exit_code == 0, res.output
    assert f"ok checks:{case_id}" in res.output, res.output
    return log


@pytest.mark.parametrize("case_id", [case.id for case in CASES])
def test_every_real_diff_case_passes(
    repo: Path, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_case(repo, case_id, monkeypatch)


def test_the_comment_carries_the_merged_verdict(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pr` + `post`: the PR is checked out first and the comment shows findings with their
    attribution, the disagreement and which lens produced no input."""
    log = _run_case(repo, "pr_posted", monkeypatch).read_text(encoding="utf-8")
    assert log.startswith("gh pr checkout 7\ngh pr comment 7 --body-file -\n")
    body = log.split("gh pr comment 7 --body-file -\n", 1)[1]
    assert body.startswith("## review_panel: request changes\n\nFour of five lenses reviewed")
    assert (
        "- **blocker** `app.py` — helper() returns the mutable module value "
        "_(raised by security, correctness)_\n"
    ) in body
    assert "### Disagreements\n- performance wants helper() inlined" in body
    assert "_Lenses: security, correctness, performance, tests; no input from api_design_\n" in body
    assert body.endswith("--- end of body\n")


def test_a_failed_checkout_reviews_nothing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _run_case(repo, "checkout_fails", monkeypatch).read_text(encoding="utf-8")
    assert log == "gh pr checkout 7\n"  # no diff, no reviewer, no comment


# --------------------------------------------------------------------------------------------------
# in-process: concurrency and resume, which no scripted answer can show
# --------------------------------------------------------------------------------------------------

ANGLE = re.compile(r"Your angle: \*\*(.+?)\*\*")
Review = dict[str, Any]
APPROVE: Review = {"verdict": "approve", "findings": []}
BLOCKER: Review = {
    "verdict": "request_changes",
    "findings": [{"severity": "blocker", "file": "app.py", "note": "helper() leaks VALUE"}],
}
MERGED: dict[str, Any] = {
    "verdict": "request_changes",
    "summary": "merged",
    "findings": [
        {
            "severity": "blocker",
            "file": "app.py",
            "note": "helper() leaks VALUE",
            "raised_by": ["security"],
        }
    ],
    "disagreements": [],
}
LOST = "lost"  # a lens whose reviewer fails outright


class PanelProvider:
    """A ``Provider`` for ``claude``: reviewers answer per lens, the chair returns ``verdict``.

    Every reviewer waits at a rendezvous until ``expected`` of them are running at once, so the
    peak concurrency the test reads is what the engine allowed, not what a sleep happened to
    overlap. A lens scripted as :data:`LOST` fails like a provider outage; ``verdict=None`` makes
    the chair fail the same way.
    """

    id = "claude"
    capabilities = STUB_CAPABILITIES

    def __init__(
        self, reviews: Mapping[str, Review | str], verdict: dict[str, Any] | None, *, expected: int
    ) -> None:
        self.reviews = dict(reviews)
        self.verdict = verdict
        self.expected = expected
        self.active = 0
        self.peak = 0
        self.all_in = anyio.Event()
        self.prompts: dict[str, str] = {}
        self.chair_prompts: list[str] = []

    async def open(
        self, *, run_id: str, workdir: str, env: Mapping[str, str], max_parallel: int
    ) -> None:
        return None

    async def healthcheck(self, *, probe: bool = False) -> ProviderHealth:
        return ProviderHealth(ok=True, sdk_version=None, auth="ok", details=())

    async def aclose(self) -> None:
        return None

    @staticmethod
    def _outage() -> AgentResult:
        error = AgentError(kind="api", message="simulated provider outage", transient=False)
        return AgentResult(status="error", text="", error=error)

    async def run(self, req: AgentRequest, emit: EmitFn) -> AgentResult:
        match = ANGLE.search(req.prompt)
        if match is None:  # the chair
            self.chair_prompts.append(req.prompt)
            if self.verdict is None:
                return self._outage()
            return AgentResult(
                status="success", text=json.dumps(self.verdict), structured=dict(self.verdict)
            )
        lens = match.group(1)
        self.prompts[lens] = req.prompt
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.active >= self.expected:
                self.all_in.set()
            with anyio.fail_after(5):
                await self.all_in.wait()
        finally:
            self.active -= 1
        answer = self.reviews.get(lens, APPROVE)
        if answer == LOST:
            return self._outage()
        assert isinstance(answer, dict)
        return AgentResult(status="success", text=json.dumps(answer), structured=dict(answer))


async def run_in_place(
    repo: Path,
    home: Path,
    provider: PanelProvider,
    *,
    resume_run_id: str | None = None,
    **inputs: Any,
) -> RunResult:
    resolved = load_workflow("review_panel", project_root=repo, home=home, config=Config())
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
        resume_run_id=resume_run_id,
        handle_signals=False,
    ).run()


def sections(prompt: str) -> list[str]:
    return re.findall(r"^## (\S+)$", prompt, flags=re.MULTILINE)


@pytest.mark.anyio
async def test_five_lenses_run_concurrently_and_the_chair_hears_each_once(
    repo: Path, home: Path
) -> None:
    provider = PanelProvider({"security": BLOCKER}, MERGED, expected=5)
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert provider.peak == 5
    assert set(provider.prompts) == set(LENSES)
    for index, lens in enumerate(LENSES):
        prompt = provider.prompts[lens]
        others = ", ".join(other for other in LENSES if other != lens)
        assert f"You are reviewer {index + 1} of 5; the others are covering {others}." in prompt
        assert "=== FULL DIFF ===" in prompt and "+def helper():" in prompt  # the real diff
    (chair,) = provider.chair_prompts
    assert sections(chair) == LENSES
    assert (
        "## security\nverdict: request_changes\n- [blocker] app.py: helper() leaks VALUE\n" in chair
    )
    assert result.outputs is not None
    assert (result.outputs["reviewed"], result.outputs["lost"]) == (5, 0)
    assert result.outputs["blockers"] == 1 and result.outputs["verdict"] == "request_changes"


@pytest.mark.anyio
async def test_a_lost_reviewer_is_named_to_the_chair_and_counted(repo: Path, home: Path) -> None:
    provider = PanelProvider({"performance": LOST}, MERGED, expected=5)
    result = await run_in_place(repo, home, provider)
    assert result.status == "succeeded", result.reason
    assert result.exit_code == 0
    assert result.steps["panel"].status == "succeeded"
    assert result.steps["panel[2]/review"].status == "failed"
    (chair,) = provider.chair_prompts
    assert sections(chair) == LENSES
    assert "## performance\nREVIEWER FAILED — no input from this lens.\n" in chair
    assert result.outputs is not None
    assert (result.outputs["reviewed"], result.outputs["lost"]) == (4, 1)


@pytest.mark.anyio
async def test_two_lenses_name_exactly_the_other(repo: Path, home: Path) -> None:
    provider = PanelProvider({}, {**MERGED, "verdict": "approve", "findings": []}, expected=2)
    result = await run_in_place(repo, home, provider, lenses='["security", "correctness"]')
    assert result.status == "succeeded", result.reason
    assert provider.peak == 2
    assert (
        "You are reviewer 1 of 2; the others are covering correctness."
        in provider.prompts["security"]
    )
    assert (
        "You are reviewer 2 of 2; the others are covering security."
        in provider.prompts["correctness"]
    )
    assert sections(provider.chair_prompts[0]) == ["security", "correctness"]
    assert result.outputs is not None and result.outputs["reviewed"] == 2


@pytest.mark.anyio
async def test_a_resumed_run_replays_the_panel_and_asks_only_the_chair_again(
    repo: Path, home: Path
) -> None:
    """The chair fails after the whole panel answered; the resume must not spend the panel twice:
    the panel's body replays from the run directory and the chair renders from what it kept."""
    first = PanelProvider({"security": BLOCKER}, None, expected=5)
    failed = await run_in_place(repo, home, first)
    assert failed.status == "failed" and len(first.prompts) == 5 and len(first.chair_prompts) == 1
    second = PanelProvider({}, MERGED, expected=5)
    result = await run_in_place(repo, home, second, resume_run_id=failed.run_id)
    assert result.status == "succeeded", result.reason
    assert second.prompts == {}  # no reviewer was asked again
    (chair,) = second.chair_prompts
    assert (
        "## security\nverdict: request_changes\n- [blocker] app.py: helper() leaks VALUE\n" in chair
    )
    assert {f"panel[{i}]/review" for i in range(5)} <= set(result.reused)
    assert result.outputs is not None
    assert (result.outputs["reviewed"], result.outputs["lost"]) == (5, 0)
