"""Actor identity: ``RAYSPEC_ACTOR`` > the OS user, and nothing a run can write to."""

from __future__ import annotations

import os
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
    actor = resolve_actor()
    assert actor.source == "os"
    assert actor.id != "me@example.invalid"


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


def test_a_machine_without_git_still_has_an_actor(
    monkeypatch: pytest.MonkeyPatch, global_email: None
) -> None:
    monkeypatch.setenv("PATH", "")
    actor = resolve_actor()
    assert actor.source == "os"


def test_the_os_user_is_not_taken_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # `source: "os"` has to mean the operating system said so — `getpass.getuser` reads
    # LOGNAME/USER first, which would let a caller name itself and have it read as derived
    pwd = pytest.importorskip("pwd", reason="the account database is POSIX-only")
    try:
        expected = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:  # a uid with no entry (a container) has only the environment to go on
        pytest.skip("this uid has no entry in the account database")
    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("LOGNAME", "someone-else")
    actor = resolve_actor()
    assert actor.source == "os"
    assert actor.id == expected


def test_the_global_git_config_is_not_an_identity_source(global_email: None) -> None:
    # `git config --global user.email …` is one command in one shell step: the file it writes
    # is the user's own, and every run on the machine can rewrite it. An identity the audited
    # code can choose is not evidence, least of all when rendered as one git derived.
    actor = resolve_actor()
    assert actor.id != "me@example.invalid"
    assert actor.source == "os"


def test_no_git_configuration_is_ever_read(
    monkeypatch: pytest.MonkeyPatch, global_email: None
) -> None:
    # not "the resolver prefers something else": it must not ask git at all, in any scope
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"the actor resolver shelled out: {args} {kwargs}")

    monkeypatch.setattr("subprocess.run", refuse)
    monkeypatch.setattr("subprocess.Popen", refuse)
    assert resolve_actor().source == "os"
