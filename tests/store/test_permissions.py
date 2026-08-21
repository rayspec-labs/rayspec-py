"""Everything the store creates is private (dirs 0700, files 0600) regardless of the umask."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from rayspec.events.model import EventType, RunEvent, StreamRecord
from rayspec.store.file import FileRunStore, open_private, secure_mkdir
from rayspec.store.model import RunRecord, StepRecord

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


@pytest.fixture(params=[0o022, 0o002, 0o000], ids=lambda u: f"umask{u:03o}")
def umask(request: pytest.FixtureRequest) -> Iterator[int]:
    old = os.umask(request.param)
    try:
        yield request.param
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _record(run_id: str = "20260820-100000-aaaa") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workflow_name="wf",
        workflow_path="wf.yaml",
        workflow_hash="x" * 64,
        project_slug="local/p-1",
        project_root="/p",
        inputs={"token": "ghp_SECRET"},
    )


def test_store_creates_private_dirs_and_files(tmp_path: Path, umask: int) -> None:
    home = tmp_path / "home"  # does not exist yet: the store creates the whole chain
    store = FileRunStore(home / "projects" / "local" / "p-1")
    run = _record()
    store.create(run)
    rec = StepRecord(path="a", id="a", kind="shell")
    store.record_step(run, rec, "secret output\n")
    store.record_step(run, StepRecord(path="b", id="b", kind="prompt"), '{"k": 1}', kind="json")
    store.append_event(run.run_id, RunEvent(type=EventType.RUN_STARTED, run_id=run.run_id))
    store.append_stream(run.run_id, "build[1]/implement", StreamRecord(kind="text", text="hi"))
    run_dir = store.run_dir(run.run_id)
    for directory in (
        home,
        home / "projects",
        home / "projects" / "local",
        store.root,
        store.runs_root,
        run_dir,
        run_dir / "steps",
        run_dir / "artifacts",
        run_dir / "tmp",
        run_dir / "steps" / "a",
        run_dir / "steps" / "build[1]" / "implement",
    ):
        assert directory.is_dir(), directory
        assert _mode(directory) == 0o700, (directory, oct(_mode(directory)))
    for file in (
        run_dir / "run.json",
        run_dir / "events.jsonl",
        run_dir / "steps" / "a" / "output.txt",
        run_dir / "steps" / "b" / "output.json",
        run_dir / "steps" / "build[1]" / "implement" / "stream.jsonl",
    ):
        assert file.is_file(), file
        assert _mode(file) == 0o600, (file, oct(_mode(file)))
    # a rewrite keeps the mode (tmp + os.replace)
    store.save(run)
    store.record_step(run, rec, "changed\n")
    assert _mode(run_dir / "run.json") == 0o600
    assert _mode(run_dir / "steps" / "a" / "output.txt") == 0o600


def test_pre_existing_directories_are_left_alone(tmp_path: Path, umask: int) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    os.chmod(home, 0o755)
    (home / "projects").mkdir(mode=0o755)
    os.chmod(home / "projects", 0o755)
    store = FileRunStore(home / "projects" / "local" / "p-1")
    store.create(_record())
    assert _mode(home) == 0o755  # not ours to tighten (an existing user dir)
    assert _mode(home / "projects") == 0o755
    assert _mode(home / "projects" / "local") == 0o700  # created by this call
    assert _mode(store.root) == 0o700
    assert _mode(store.runs_root) == 0o700


def test_helpers(tmp_path: Path, umask: int) -> None:
    target = tmp_path / "a" / "b" / "c"
    secure_mkdir(target)
    secure_mkdir(target)  # idempotent
    assert _mode(target) == 0o700 and _mode(tmp_path / "a") == 0o700
    path = target / "f.txt"
    with open_private(path, "w") as fh:
        fh.write("x")
    assert _mode(path) == 0o600
    with open_private(path, "a") as fh:
        fh.write("y")
    assert path.read_text() == "xy" and _mode(path) == 0o600


WORKFLOW = """\
rayspec: 1
name: secrets
isolation: none
inputs:
  token:
    type: string
steps:
  - id: a
    shell: echo "token={{ inputs.token }}" >&2; echo "token={{ inputs.token }}"
"""


def test_rayspec_run_leaves_nothing_world_readable(
    tmp_path: Path, umask: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repro: a fresh RAYSPEC_HOME under umask 022/002/000, `rayspec run … --exec-shell`
    with a secret input — every directory and file created under the home is private, including
    the ones the workdir lock creates first (home, projects/<slug>, locks/), the lock file, the
    step's context.json and its stdout/stderr logs."""
    from typer.testing import CliRunner

    from rayspec.cli.app import app

    home = tmp_path / "home"
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    project = tmp_path / "proj"
    (project / ".rayspec" / "workflows").mkdir(parents=True)
    (project / ".rayspec" / "workflows" / "secrets.yaml").write_text(WORKFLOW)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "secrets",
            "--dry-run",
            "--exec-shell",
            "--no-interactive",
            "--input",
            "token=ghp_SECRET",
            "--root",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.output
    leaks: list[str] = []
    for path in [*sorted(home.rglob("*")), home]:
        mode = _mode(path)
        expected = 0o700 if path.is_dir() else 0o600
        if mode != expected:
            leaks.append(f"{path.relative_to(tmp_path)} {oct(mode)}")
    assert not leaks, "\n".join(leaks)
    # the expected writers actually ran
    names = {p.name for p in home.rglob("*")}
    assert {"context.json", "stdout.log", "stderr.log", "run.json"} <= names, names
    assert any(p.suffix == ".lock" for p in home.rglob("*")), "no lock file written"


def test_open_private_does_not_follow_a_symlink(tmp_path: Path, umask: int) -> None:
    # a pre-planted symlink at the (predictable) tmp path must not redirect the write into a
    # file of someone else's choosing / mode
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    os.chmod(victim, 0o644)
    link = tmp_path / "run.json.1.tmp"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        open_private(link, "w")
    assert victim.read_text() == "keep" and _mode(victim) == 0o644


GIT_WORKFLOW = """\
rayspec: 1
name: wt
steps:
  - id: a
    shell: echo hi
"""


def test_worktree_run_leaves_nothing_world_readable_outside_the_checkout(
    tmp_path: Path, umask: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default-isolation run on a git project — the worktree is the
    first writer under a fresh RAYSPEC_HOME — leaves every rayspec-created directory 0700 and
    file 0600; only the git checkout inside ``worktrees/<name>/`` keeps git's modes."""
    import subprocess

    from typer.testing import CliRunner

    from rayspec.cli.app import app

    home = tmp_path / "home"
    monkeypatch.setenv("RAYSPEC_HOME", str(home))
    project = tmp_path / "proj"
    (project / ".rayspec" / "workflows").mkdir(parents=True)
    (project / ".rayspec" / "workflows" / "wt.yaml").write_text(GIT_WORKFLOW)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=project, check=True, env=env)
    result = CliRunner().invoke(
        app, ["run", "wt", "--dry-run", "--exec-shell", "--root", str(project)]
    )
    assert result.exit_code == 0, result.output
    worktrees = next(home.glob("projects/*/*/worktrees"))
    checkouts = [p for p in worktrees.iterdir() if p.is_dir()]
    assert len(checkouts) == 1, checkouts
    leaks: list[str] = []
    for path in [*sorted(home.rglob("*")), home]:
        if path == checkouts[0] or checkouts[0] in path.parents:
            continue  # git's checkout: git's modes
        mode = _mode(path)
        expected = 0o700 if path.is_dir() else 0o600
        if mode != expected:
            leaks.append(f"{path.relative_to(tmp_path)} {oct(mode)}")
    assert not leaks, "\n".join(leaks)
    assert _mode(worktrees) == 0o700 and _mode(home) == 0o700
