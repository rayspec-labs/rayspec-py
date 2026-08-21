# SPDX-License-Identifier: Apache-2.0
"""Who acted — the local identity rayspec stamps on a run and on an approval decision.

Module boundary: resolving an **identity** from the process environment, the repository's git
configuration and the operating system. Three rules hold here and nowhere else has to repeat
them:

* it is an identity, never a credential — no token, key or password is read, derived or
  recorded (a provider account is taken only from variables that *name* an account, never from
  the ones that carry the secret);
* no source is one the run can write to — in particular a repository's own git configuration is
  never read, because a run's worktree shares it with the repository (see :func:`git_email`);
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

from rayspec.store.model import ActorInfo
from rayspec.textsafe import safe_text

#: Environment variable that overrides the resolved identity (a scheduler sets it).
ACTOR_ENV = "RAYSPEC_ACTOR"
#: Longest identity recorded; anything longer is truncated with an ellipsis.
MAX_ACTOR_LEN = 256
#: Seconds a ``git config`` lookup may take before the resolver gives up on it.
GIT_TIMEOUT_S = 5.0
#: The git configuration scopes an identity may come from, in order. Repository-local
#: configuration is absent on purpose — see :func:`git_email`.
GIT_SCOPES: tuple[str, ...] = ("--global", "--system")

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


def git_email() -> str | None:
    """``user.email`` from the git configuration **outside any repository**.

    :data:`GIT_SCOPES` in order — the user's own configuration, then the machine's. The
    repository is deliberately never asked: a run's worktree shares ``.git/config`` with the
    repository it was made from, so one ``git config user.email …`` in a shell step or by an
    agent would pick the identity stamped on the human's next approval. An identity the audited
    code can choose is worse than no identity at all, because the ledger presents it as one git
    itself derived. Somebody who wants a per-project identity sets :data:`ACTOR_ENV`.

    Never raises: a missing ``git`` binary, an unreadable configuration or a timeout is simply
    "no answer from git".
    """
    try:
        from rayspec.workspace.errors import GitError
        from rayspec.workspace.git import run_git
    except ImportError:  # the workspace layer is optional; the OS user still answers
        return None
    for scope in GIT_SCOPES:
        try:
            result = run_git(
                ["config", scope, "--get", "user.email"], None, check=False, timeout=GIT_TIMEOUT_S
            )
        except (GitError, OSError):
            return None
        if result.ok:
            identity = clean_identity(result.stdout)
            if identity is not None:
                return identity
    return None


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


def resolve_actor(*, env: Mapping[str, str] | None = None) -> ActorInfo:
    """Who is running this — :data:`ACTOR_ENV`, else the git identity, else the OS user.

    Every source is one a *run* cannot reach: this process's environment, the user's own git
    configuration (:func:`git_email`, never a repository's) and the operating-system user.
    There is deliberately no parameter for a directory, because the only directories on offer
    at the call sites are ones the run itself had write access to.

    The result also carries the CI system, when this is one, and the provider accounts the
    environment names. It is an identity for a log, not an authorisation: nothing in rayspec
    grants a permission because of it.
    """
    env = os.environ if env is None else env
    identity = clean_identity(env.get(ACTOR_ENV))
    source = "env"
    if identity is None:
        identity, source = git_email(), "git"
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
    "GIT_SCOPES",
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
