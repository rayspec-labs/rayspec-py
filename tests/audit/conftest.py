"""Fixtures for the audit tests: a deterministic identity environment and a real git repo.

The actor resolver reads the process environment and the repository's git configuration, so
every test here starts from a *cleared* identity: no ``RAYSPEC_ACTOR``, no CI markers and no
provider-account variables leaking in from the developer's shell or from CI itself (this suite
runs on GitHub Actions, where ``GITHUB_ACTIONS`` is set for real).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rayspec.actor import CI_ENV_MARKERS, PROVIDER_ACCOUNT_ENV
from rayspec.store.file import FileRunStore

GIT_ENV = {
    "GIT_AUTHOR_NAME": "rayspec-test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "rayspec-test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def clean_identity_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No inherited identity: the resolver must see only what a test puts there."""
    monkeypatch.delenv("RAYSPEC_ACTOR", raising=False)
    monkeypatch.delenv("RAYSPEC_AUDIT_LOG", raising=False)
    monkeypatch.delenv("RAYSPEC_PUSH_BRANCH", raising=False)
    for name, _label in CI_ENV_MARKERS:
        monkeypatch.delenv(name, raising=False)
    for names in PROVIDER_ACCOUNT_ENV.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "gitconfig-global"
    empty.write_text("")
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))


def git(*args: str, cwd: Path) -> str:
    """Run git in ``cwd`` and return stripped stdout (raises on failure)."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with one commit and a repo-local ``user.email``."""
    path = tmp_path / "repo"
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "maintainer@example.invalid", cwd=path)
    git("config", "user.name", "Maintainer", cwd=path)
    (path / "a.txt").write_text("a\n")
    git("add", ".", cwd=path)
    git("commit", "-q", "-m", "first", cwd=path)
    return path


GATE_WORKFLOW = """
rayspec: 1
name: gate
isolation: none
steps:
  - {id: a, shell: echo hello}
  - id: plan
    needs: [a]
    agent: {provider: stub}
    prompt: "plan for {{ steps.a.output }}"
  - {id: ok, needs: [plan], approve: "ship it?"}
  - {id: b, needs: [ok], shell: "echo {{ steps.ok.output }}"}
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project whose only workflow pauses at an approval gate."""
    root = tmp_path / "proj"
    workflows = root / ".rayspec" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "gate.yaml").write_text(GATE_WORKFLOW, encoding="utf-8")
    return root


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def only_store(home: Path) -> FileRunStore:
    """The one project run store under ``home`` (the CLI created it)."""
    (slug_dir,) = [p for p in (home / "projects").glob("*/*") if (p / "runs").is_dir()]
    return FileRunStore(slug_dir)
