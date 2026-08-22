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


def test_an_env_file_cannot_name_the_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    # the whole class: a variable rayspec copied out of a `.env` is configuration, and both of
    # those files are files a `shell:` step can write
    from rayspec.procenv import forget_env_file_values, note_env_file_values

    monkeypatch.setenv(ACTOR_ENV, "planted@corp.invalid")
    note_env_file_values({ACTOR_ENV: "planted@corp.invalid"}, origin="/home/.rayspec/.env")
    try:
        actor = resolve_actor()
        assert actor.id != "planted@corp.invalid"
        assert actor.source == "os"
        # refused, not swallowed: the claim is on the record, marked as a claim
        assert actor.declared_id == "planted@corp.invalid"
    finally:
        forget_env_file_values()


def test_an_env_file_cannot_forge_the_ci_system_or_a_provider_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # not only RAYSPEC_ACTOR: every field of the record is evidence, so every field is resolved
    # from what the operator set. A planted GITHUB_ACTIONS would make a laptop run read as CI.
    from rayspec.procenv import forget_env_file_values, note_env_file_values

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ANTHROPIC_ACCOUNT", "security-team")
    note_env_file_values(
        {"GITHUB_ACTIONS": "true", "ANTHROPIC_ACCOUNT": "security-team"},
        origin="/project/.rayspec/.env",
    )
    try:
        actor = resolve_actor()
        assert actor.ci is None
        assert actor.provider_accounts == {}
    finally:
        forget_env_file_values()


def test_the_shell_still_wins_over_an_env_file_of_the_same_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a `.env` never overrides an exported variable, so nothing is recorded for it and the
    # operator's own identity keeps working — the control must not block the permitted case
    from rayspec.procenv import forget_env_file_values, note_env_file_values

    note_env_file_values({ACTOR_ENV: "planted@corp.invalid"}, origin="/project/.rayspec/.env")
    monkeypatch.setenv(ACTOR_ENV, "operator@example.invalid")
    try:
        actor = resolve_actor()
        assert actor.id == "operator@example.invalid"
        assert actor.source == "env"
        assert actor.declared_id is None
    finally:
        forget_env_file_values()


def test_an_explicit_env_mapping_is_taken_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    # `resolve_actor(env=…)` is the caller saying "this mapping is the operator's environment";
    # the subtraction applies to the process environment it would otherwise have read
    actor = resolve_actor(env={ACTOR_ENV: "scheduler@example.invalid"})
    assert actor.id == "scheduler@example.invalid"
    assert actor.source == "env"


def _no_such_uid(_uid: int) -> object:
    """``pwd.getpwuid`` on a host where this uid has no entry — a container's ordinary case."""
    raise KeyError("getpwuid(): uid not found")


def test_a_uid_without_an_account_entry_is_not_named_by_a_planted_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the account database is the first choice and it can simply be absent: a container running
    # under a uid with no passwd entry is the ordinary case, not the exotic one. The fallback
    # must go through `operator_env` like everything else here — `getpass.getuser` reads
    # `os.environ` directly, which is where a `.env` a step wrote has already landed.
    from rayspec.procenv import forget_env_file_values, note_env_file_values

    pwd = pytest.importorskip("pwd", reason="the account database is POSIX-only")
    monkeypatch.setattr(pwd, "getpwuid", _no_such_uid)
    planted = {name: "planted@corp.invalid" for name in ("USER", "LOGNAME", "LNAME", "USERNAME")}
    for name, value in planted.items():
        monkeypatch.setenv(name, value)
    note_env_file_values(planted, origin="/home/.rayspec/.env")
    try:
        actor = resolve_actor()
        assert actor.id != "planted@corp.invalid"
        assert (actor.id, actor.source) == ("unknown", "unknown")
    finally:
        forget_env_file_values()


def test_the_os_user_is_never_asked_of_getpass(monkeypatch: pytest.MonkeyPatch) -> None:
    # not "the resolver prefers the account database": `getpass.getuser` may not be on the path
    # at all, because it is the one lookup here that bypasses `operator_env` by construction
    import getpass

    pwd = pytest.importorskip("pwd", reason="the account database is POSIX-only")
    monkeypatch.setattr(pwd, "getpwuid", _no_such_uid)

    def refuse() -> str:
        raise AssertionError("the actor resolver read os.environ through getpass.getuser")

    monkeypatch.setattr(getpass, "getuser", refuse)
    for name in ("USER", "LOGNAME", "LNAME", "USERNAME"):
        monkeypatch.delenv(name, raising=False)
    assert resolve_actor().source == "unknown"


def test_the_operators_own_user_still_answers_without_an_account_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # narrowing the source must not take away the case it exists for: `$USER` as the OPERATOR
    # exported it is still a source the audited run cannot write
    pwd = pytest.importorskip("pwd", reason="the account database is POSIX-only")
    monkeypatch.setattr(pwd, "getpwuid", _no_such_uid)
    monkeypatch.setenv("USER", "operator")
    actor = resolve_actor()
    assert actor.id == "operator"
    assert actor.source == "os"
