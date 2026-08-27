# SPDX-License-Identifier: Apache-2.0
"""The bundled `explore_block` — a read-only plan-mode fan-out (B12, PRD-09 §explore).

Run standalone (it is normally `include:`d) through `rayspec test --exec-shell` so the `collect`
python really reshapes the per-question explorer reports: an index-aligned `reports` list (null
where an explorer failed), `answered`, and `lost`. The stub scripts the `scan[*]/inspect` prompts.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.cli.app import app
from rayspec.testing import load_checks

HERE = Path(__file__).resolve().parent
SUITE = HERE / "explore_block"
CASES = load_checks(SUITE / "checks.yaml")
runner = CliRunner()

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("helper = 1\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    shutil.copy(SUITE / "checks.yaml", root / "checks.yaml")
    shutil.copytree(SUITE / "stubs", root / "stubs")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.mark.parametrize("case_id", [case.id for case in CASES])
def test_every_explore_block_case_passes(
    repo: Path, case_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    res = runner.invoke(app, ["test", "--root", str(repo), "--exec-shell", "--case", case_id])
    assert res.exit_code == 0, res.output
    assert f"ok checks:{case_id}" in res.output, res.output
    # a read-only fan-out changes nothing in the tree
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert status == "", status
