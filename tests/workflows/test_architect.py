"""The bundled ``architect`` workflow against a real tree.

Declarative ``exec_shell: true`` cases from ``architect/checks.yaml``, driven through
``rayspec test --exec-shell`` with the stub provider on a throw-away repository of known shape —
the things the dry run in ``tests/workflows/checks.yaml`` can only simulate: which directories the
area scan picks and in what order, what each surveyor and the architect are told, that a lost
surveyor is counted rather than fatal, that the token ceiling really stops the run, and the two
acceptance criteria of the PRD: the report lands in the run directory, and nothing in the
repository changes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.testing import load_checks
from rayspec.testing.runner import run_case
from rayspec.testing.spec import discover_suites

HERE = Path(__file__).resolve().parent
SUITE = HERE / "architect"
CASES = load_checks(SUITE / "checks.yaml")
runner = CliRunner()

GIT_ENV = {
    "GIT_AUTHOR_NAME": "rayspec-test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "rayspec-test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}

#: relative path → number of lines; the area scan must rank core > api > tests > util > scripts
SOURCE_FILES = {
    "src/app/core/a.py": 25,
    "src/app/core/b.py": 20,
    "src/app/core/c.py": 15,
    "src/app/api/x.py": 18,
    "src/app/api/y.py": 12,
    "tests/test_x.py": 20,
    "src/app/util/h.py": 10,
    "scripts/run.sh": 5,
    "build/out.py": 40,  # tracked, but a generated tree the scan leaves out
}


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def lines(n: int) -> str:
    return "".join(f"x{i} = {i}\n" for i in range(n))


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "pyproject.toml", '[project]\nname = "demo"\n')
    write(root, "docs/readme.md", "not source\n")
    write(root, ".gitignore", "node_modules/\n")
    write(root, "node_modules/pkg/index.js", lines(500))  # ignored: never counted
    for name, count in SOURCE_FILES.items():
        write(root, name, lines(count))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def bare_repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "bare"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "README.md", "# nothing to survey\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def _run_case(root: Path, case_id: str, monkeypatch: pytest.MonkeyPatch) -> str:
    shutil.copy(SUITE / "checks.yaml", root / "checks.yaml")
    shutil.copytree(SUITE / "stubs", root / "stubs")
    git(root, "add", "-A")  # the suite is part of the tree, so a clean status means "untouched"
    git(root, "commit", "-q", "-m", "suite")
    monkeypatch.chdir(root)
    res = runner.invoke(app, ["test", "--root", str(root), "--exec-shell", "--case", case_id])
    assert res.exit_code == 0, res.output
    assert f"ok checks:{case_id}" in res.output, res.output
    # the PRD's acceptance criterion: a survey modifies no tracked file — nor adds one
    assert git(root, "status", "--porcelain") == ""
    return res.output


@pytest.mark.parametrize("case_id", [case.id for case in CASES])
def test_every_real_tree_case_passes(
    request: pytest.FixtureRequest, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = request.getfixturevalue("bare_repo" if case_id == "no_sources" else "repo")
    _run_case(root, case_id, monkeypatch)


def test_the_report_lands_in_the_run_directory(
    repo: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report is a file under the run's artifacts/, never anything in the repository.

    Driven through the testing API rather than the CLI: `rayspec test` deletes a passing case's
    run directory, and the file that must exist lives in it."""
    shutil.copy(SUITE / "checks.yaml", repo / "checks.yaml")
    shutil.copytree(SUITE / "stubs", repo / "stubs")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "suite")
    monkeypatch.chdir(repo)
    (suite,) = discover_suites(repo)
    (case,) = [c for c in suite.checks if c.id == "survey_defaults"]
    result = run_case(suite, case, home=home, exec_shell=True, keep_run_dir=True)
    assert result.ok, result.failures
    assert result.run_dir is not None and result.run_dir.is_relative_to(home)
    text = (result.run_dir / "artifacts" / "architecture.md").read_text(encoding="utf-8")
    assert text.startswith("# Architecture: coupling\ncore is imported by api and tests")
    assert git(repo, "status", "--porcelain") == ""
    assert not list(repo.rglob("architecture.md"))
