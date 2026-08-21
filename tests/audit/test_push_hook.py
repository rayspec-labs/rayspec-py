"""The push hook end to end: it fires on pause and on finish, and never changes the run."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.workspace.git import PUSH_ENV, run_git

from .conftest import git, only_store

WORKFLOW = """
rayspec: 1
name: gate
steps:
  - id: work
    shell: 'echo work > note.txt && git add -A && git commit -q -m note'
  - id: gate
    needs: [work]
    approve: "ship?"
"""


DIRTY_WORKFLOW = """
rayspec: 1
name: dirty
steps:
  - id: work
    shell: 'echo work > note.txt'
  - id: gate
    needs: [work]
    approve: "ship?"
"""


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    path = tmp_path / "origin.git"
    path.mkdir()
    git("init", "-q", "--bare", "-b", "main", cwd=path)
    return path


@pytest.fixture
def gitproject(tmp_path: Path, origin: Path) -> Path:
    root = tmp_path / "proj"
    workflows = root / ".rayspec" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "gate.yaml").write_text(WORKFLOW)
    (workflows / "dirty.yaml").write_text(DIRTY_WORKFLOW)
    git("init", "-q", "-b", "main", cwd=root)
    git("config", "user.email", "maintainer@example.invalid", cwd=root)
    git("config", "user.name", "Maintainer", cwd=root)
    git("add", ".", cwd=root)
    git("commit", "-q", "-m", "first", cwd=root)
    git("remote", "add", "origin", str(origin), cwd=root)
    git("push", "-q", "origin", "main", cwd=root)
    return root


def _remote_branches(origin: Path) -> list[str]:
    out = run_git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], origin)
    return out.stdout.split()


def test_the_branch_is_published_on_pause_and_on_finish(
    cli: CliRunner, gitproject: Path, origin: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PUSH_ENV, "1")
    result = cli.invoke(app, ["run", "gate", "--root", str(gitproject), "--no-interactive"])
    assert result.exit_code == 3, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    branch = store.load(run_id).workspace.branch
    assert branch and branch.startswith("rayspec/gate-")
    assert branch in _remote_branches(origin), "the paused run's branch must be on the remote"
    before = run_git(["rev-parse", branch], origin).stdout
    approve = cli.invoke(app, ["approve", run_id, "ok", "--root", str(gitproject)])
    assert approve.exit_code == 0, approve.output
    assert branch in _remote_branches(origin)
    assert run_git(["rev-parse", branch], origin).stdout == before


def test_no_push_without_the_opt_in(
    cli: CliRunner, gitproject: Path, origin: Path, home: Path
) -> None:
    result = cli.invoke(app, ["run", "gate", "--root", str(gitproject), "--no-interactive"])
    assert result.exit_code == 3, result.output
    assert _remote_branches(origin) == ["main"]


def test_a_failed_push_is_only_a_warning(
    cli: CliRunner, gitproject: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PUSH_ENV, "1")
    git("remote", "set-url", "origin", str(gitproject / "gone.git"), cwd=gitproject)
    result = cli.invoke(app, ["run", "gate", "--root", str(gitproject), "--no-interactive"])
    assert result.exit_code == 3, result.output  # unchanged: still a plain pause
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    record = store.load(run_id)
    assert record.status.value == "paused"
    warnings = [
        e for e in store.read_events(run_id) if e.type.value == "warning" and "push" in str(e.data)
    ]
    assert warnings, "a push that fails must leave a warning behind"


def test_an_in_place_run_is_never_pushed(
    cli: CliRunner, gitproject: Path, origin: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PUSH_ENV, "1")
    result = cli.invoke(
        app, ["run", "gate", "--root", str(gitproject), "--no-worktree", "--no-interactive"]
    )
    assert result.exit_code == 3, result.output
    assert _remote_branches(origin) == ["main"], "an in-place run pushes the user's own branch"


def test_uncommitted_work_is_called_out(
    cli: CliRunner, gitproject: Path, origin: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rayspec never commits: a run whose agent left work in the worktree publishes a branch that
    # does not contain it, and the user has to be told rather than left believing it is backed up
    monkeypatch.setenv(PUSH_ENV, "1")
    result = cli.invoke(app, ["run", "dirty", "--root", str(gitproject), "--no-interactive"])
    assert result.exit_code == 3, result.output
    store = only_store(home)
    (run_id,) = store.list_run_ids()
    branch = store.load(run_id).workspace.branch
    assert branch and branch in _remote_branches(origin)
    assert not run_git(["show", f"{branch}:note.txt"], origin, check=False).ok
    warnings = [
        e
        for e in store.read_events(run_id)
        if e.type.value == "warning" and "uncommitted" in str(e.data)
    ]
    assert warnings, "a push that published none of the work must say so"
