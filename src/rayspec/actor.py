# SPDX-License-Identifier: Apache-2.0
"""Who acted — the local identity rayspec stamps on a run and on an approval decision.

Module boundary: resolving an **identity** from the process environment, the repository's git
configuration and the operating system. Three rules hold here and nowhere else has to repeat
them:

* it is an identity, never a credential — no token, key or password is read, derived or
  recorded (a provider account is taken only from variables that *name* an account, never from
  the ones that carry the secret);
* nothing here opens a socket: every answer is available on the machine the run happens on;
* it never raises — an unreadable git config or a missing ``git`` binary simply falls through
  to the next source.

The record shape (:class:`~rayspec.store.model.ActorInfo`) lives with the rest of ``run.json``;
this module only fills it in.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Mapping
from pathlib import Path

from rayspec.store.model import ActorInfo
from rayspec.textsafe import safe_text

#: Environment variable that overrides the resolved identity (a scheduler sets it).
ACTOR_ENV = "RAYSPEC_ACTOR"
#: Longest identity recorded; anything longer is truncated with an ellipsis.
MAX_ACTOR_LEN = 256
#: Seconds a ``git config`` lookup may take before the resolver gives up on it.
GIT_TIMEOUT_S = 5.0

#: ``(variable, label)`` pairs, most specific first: the first variable whose value is truthy
#: names the CI system. ``CI`` is last because every one of the others sets it too.
CI_ENV_MARKERS: tuple[tuple[str, str], ...] = (
    ("GITHUB_ACTIONS", "github-actions"),
    ("GITLAB_CI", "gitlab-ci"),
    ("BUILDKITE", "buildkite"),
    ("CIRCLECI", "circleci"),
    ("TF_BUILD", "azure-pipelines"),
    ("JENKINS_URL", "jenkins"),
    ("TEAMCITY_VERSION", "teamcity"),
    ("CI", "ci"),
)

#: Provider id → environment variables that NAME an account or organisation. Deliberately not
#: the API-key variables: their value is a credential, and even its presence is not an identity.
PROVIDER_ACCOUNT_ENV: Mapping[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_ACCOUNT",),
    "codex": ("OPENAI_ORG_ID", "OPENAI_ORGANIZATION"),
}

#: Environment variables consulted for the OS user when :func:`getpass.getuser` cannot answer.
_USER_ENV = ("USER", "LOGNAME", "USERNAME")

#: Values that mean "no" in an environment flag.
_FALSY = frozenset({"", "0", "false", "no", "off"})


def clean_identity(value: str | None) -> str | None:
    """One line of plain text, capped at :data:`MAX_ACTOR_LEN` — or ``None`` when empty.

    An identity may come from an environment variable a scheduler set, so it is untrusted text:
    control characters and escape sequences are removed before it can reach a terminal.
    """
    if value is None:
        return None
    text = " ".join(safe_text(value, keep_newlines=False).split())
    if not text:
        return None
    if len(text) > MAX_ACTOR_LEN:
        text = text[: MAX_ACTOR_LEN - 1] + "…"
    return text


def _truthy(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name)
    return value is not None and value.strip().lower() not in _FALSY


def detect_ci(env: Mapping[str, str] | None = None) -> str | None:
    """The CI system this process runs under (``github-actions``, …), ``None`` on a laptop."""
    env = os.environ if env is None else env
    for name, label in CI_ENV_MARKERS:
        if _truthy(env, name):
            return label
    return None


def provider_accounts(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``{provider id: account}`` for the accounts the environment NAMES (never a credential)."""
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    for provider, names in PROVIDER_ACCOUNT_ENV.items():
        for name in names:
            account = clean_identity(env.get(name))
            if account is not None:
                out[provider] = account
                break
    return out


def git_email(workdir: Path | None = None) -> str | None:
    """``git config --get user.email`` in ``workdir``; ``None`` when git or the value is absent.

    Never raises: a missing ``git`` binary, an unreadable directory or a timeout is simply "no
    answer from git".
    """
    try:
        from rayspec.workspace.errors import GitError
        from rayspec.workspace.git import run_git
    except ImportError:  # the workspace layer is optional; the OS user still answers
        return None
    try:
        result = run_git(
            ["config", "--get", "user.email"], workdir, check=False, timeout=GIT_TIMEOUT_S
        )
    except (GitError, OSError):
        return None
    if not result.ok:
        return None
    return clean_identity(result.stdout)


def os_user(env: Mapping[str, str] | None = None) -> str | None:
    """The operating-system user, from :func:`getpass.getuser` or the usual variables."""
    env = os.environ if env is None else env
    try:
        name = clean_identity(getpass.getuser())
    except (KeyError, OSError):
        name = None
    if name is not None:
        return name
    for variable in _USER_ENV:
        name = clean_identity(env.get(variable))
        if name is not None:
            return name
    return None


def resolve_actor(
    *, workdir: Path | None = None, env: Mapping[str, str] | None = None
) -> ActorInfo:
    """Who is running this — :data:`ACTOR_ENV`, else the repo's git email, else the OS user.

    ``workdir`` is where the git configuration is read (the run's working directory, so a
    repository-local ``user.email`` wins over the global one). The result also carries the CI
    system, when this is one, and the provider accounts the environment names. It is an
    identity for a log, not an authorisation: nothing in rayspec grants a permission because of
    it.
    """
    env = os.environ if env is None else env
    identity = clean_identity(env.get(ACTOR_ENV))
    source = "env"
    if identity is None:
        identity, source = git_email(workdir), "git"
    if identity is None:
        identity, source = os_user(env), "os"
    if identity is None:
        identity, source = "unknown", "unknown"
    return ActorInfo(
        id=identity,
        source=source,
        ci=detect_ci(env),
        provider_accounts=provider_accounts(env),
    )


__all__ = [
    "ACTOR_ENV",
    "CI_ENV_MARKERS",
    "GIT_TIMEOUT_S",
    "MAX_ACTOR_LEN",
    "PROVIDER_ACCOUNT_ENV",
    "clean_identity",
    "detect_ci",
    "git_email",
    "os_user",
    "provider_accounts",
    "resolve_actor",
]
