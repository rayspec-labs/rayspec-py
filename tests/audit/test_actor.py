"""Actor identity: ``RAYSPEC_ACTOR`` > ``git config user.email`` > the OS user."""

from __future__ import annotations

from pathlib import Path

import pytest

from rayspec.actor import ACTOR_ENV, MAX_ACTOR_LEN, resolve_actor
from rayspec.store.model import ActorInfo


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setenv(ACTOR_ENV, "nightly-bot")
    actor = resolve_actor()
    assert isinstance(actor, ActorInfo)
    assert actor.id == "nightly-bot"
    assert actor.source == "env"


def test_git_email_is_the_second_choice(global_email: None) -> None:
    actor = resolve_actor()
    assert actor.id == "me@example.invalid"
    assert actor.source == "git"


def test_a_repository_cannot_name_the_actor(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a worktree shares .git/config with its repository, so anything the run can write there is
    # chosen by the code being audited — the resolver must not read it
    monkeypatch.chdir(repo)
    actor = resolve_actor()
    assert actor.id != "maintainer@example.invalid"
    assert actor.source == "os"


def test_os_user_is_the_last_resort(tmp_path: Path) -> None:
    actor = resolve_actor()
    assert actor.source == "os"
    assert actor.id


def test_blank_override_is_ignored(monkeypatch: pytest.MonkeyPatch, global_email: None) -> None:
    monkeypatch.setenv(ACTOR_ENV, "   ")
    assert resolve_actor().source == "git"


def test_identity_is_sanitised_and_capped(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setenv(ACTOR_ENV, "bo\x1b[31mt\n" + "x" * 400)
    actor = resolve_actor()
    assert "\x1b" not in actor.id and "\n" not in actor.id
    assert len(actor.id) <= MAX_ACTOR_LEN


def test_ci_is_detected(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    assert resolve_actor().ci is None
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert resolve_actor().ci == "github-actions"


def test_generic_ci_marker(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setenv("CI", "true")
    assert resolve_actor().ci == "ci"
    monkeypatch.setenv("CI", "false")
    assert resolve_actor().ci is None


def test_provider_account_is_recorded_but_never_a_credential(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_ACCOUNT", "acme-research")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-value")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-another-secret")
    actor = resolve_actor()
    assert actor.provider_accounts == {"claude": "acme-research"}
    dumped = actor.model_dump_json()
    assert "sk-ant-super-secret-value" not in dumped
    assert "sk-proj-another-secret" not in dumped


def test_a_missing_git_binary_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, global_email: None
) -> None:
    monkeypatch.setenv("PATH", "")
    actor = resolve_actor()
    assert actor.source == "os"
