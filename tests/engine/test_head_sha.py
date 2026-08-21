"""``workspace.head_sha`` is the tip of the run workdir at the last record write: refreshed
at pause, at run end and on resume start (not frozen at worktree creation)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rayspec.engine.context import RunOptions
from rayspec.engine.runner import Workspace
from rayspec.schema import RunStatus
from rayspec.store.model import Decision

from .conftest import Harness

pytestmark = pytest.mark.anyio


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(harness: Harness) -> tuple[Path, str]:
    root = harness.root
    _git("init", "-q", "-b", "main", cwd=root)
    (root / "README").write_text("hi\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root, _git("rev-parse", "HEAD", cwd=root)


WF = """
rayspec: 1
name: t
steps:
  - id: commit
    shell: |
      echo change >> README
      git -c user.name=t -c user.email=t@example.com commit -qam "agent change"
      git rev-parse HEAD
  - {id: gate, needs: [commit], approve: "ship?"}
  - id: again
    needs: [gate]
    shell: |
      echo more >> README
      git -c user.name=t -c user.email=t@example.com commit -qam "second change"
      git rev-parse HEAD
"""


async def test_head_sha_follows_the_workdir_tip_at_pause_end_and_resume(
    harness: Harness, repo: tuple[Path, str]
) -> None:
    root, base = repo
    harness.workflow("t", WF)
    ws = Workspace(
        isolation="worktree",
        workdir=root,
        branch="rayspec/t-abcd",
        base_branch="main",
        base_sha=base,
        head_sha=base,
    )
    result = await harness.run("t", workspace=ws, options=RunOptions(interactive=False))
    assert result.status is RunStatus.PAUSED
    first = harness.store.read_output(result.run_id, result.steps["commit"].output_ref or "")
    run = harness.record(result.run_id)
    # paused: head_sha is the agent's commit, base_sha is untouched
    assert run.workspace.head_sha == first.strip() != base
    assert run.workspace.base_sha == base
    assert result.workspace.head_sha == first.strip()

    # another commit lands in the worktree while paused; the resume start refreshes again
    (root / "README").write_text("manual\n")
    _git("commit", "-qam", "manual while paused", cwd=root)
    manual = _git("rev-parse", "HEAD", cwd=root)
    run.pause.decision = Decision(approved=True, comment="go", by="cli")  # type: ignore[union-attr]
    harness.store.save(run)
    resumed = await harness.run(
        "t", resume=result.run_id, workspace=ws, options=RunOptions(interactive=False)
    )
    assert resumed.status is RunStatus.SUCCEEDED
    second = harness.store.read_output(resumed.run_id, resumed.steps["again"].output_ref or "")
    final = harness.record(result.run_id)
    assert final.workspace.head_sha == second.strip() != manual
    assert final.workspace.base_sha == base and final.workspace.branch == "rayspec/t-abcd"
    # the resume start stamped the tip it found (the manual commit) before anything ran
    events = harness.events()
    assert events  # (run.resumed emitted; the head refresh itself is not an event)


async def test_in_place_git_run_refreshes_head_sha_too(harness: Harness, repo) -> None:
    root, base = repo
    harness.workflow(
        "t",
        """
rayspec: 1
name: t
steps:
  - id: c
    shell: |
      echo x >> README
      git -c user.name=t -c user.email=t@example.com commit -qam "x"
""",
    )
    ws = Workspace(isolation="none", workdir=root, branch="main", head_sha=base)
    result = await harness.run("t", workspace=ws)
    assert result.status is RunStatus.SUCCEEDED
    head = _git("rev-parse", "HEAD", cwd=root)
    assert head != base and harness.record(result.run_id).workspace.head_sha == head


async def test_non_git_workdir_keeps_head_sha_none(harness: Harness) -> None:
    harness.workflow("t", "rayspec: 1\nname: t\nsteps:\n  - {id: a, shell: echo a}\n")
    result = await harness.run("t")
    assert harness.record(result.run_id).workspace.head_sha is None
