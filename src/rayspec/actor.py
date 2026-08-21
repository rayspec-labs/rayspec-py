# SPDX-License-Identifier: Apache-2.0
"""Who acted — the local identity rayspec stamps on a run and on an approval decision.

Module boundary: resolving an **identity** from the process environment and the operating
system. Four rules hold here and nowhere else has to repeat them:

* it is an identity, never a credential — no token, key or password is read, derived or
  recorded (a provider account is taken only from variables that *name* an account, never from
  the ones that carry the secret);
* **an identity is only evidence if the audited code could not have chosen it.** That is one
  rule, and it is a rule about the *source*, so it does not need re-deciding per attack. Two
  sources pass it and everything else is refused:

  - the process environment **as the operator set it** — :func:`rayspec.procenv.operator_env`,
    which is ``os.environ`` minus every variable rayspec itself copied out of a ``.env`` file;
  - the operating-system user (:func:`os_user`).

  Refused, and why: any **git configuration**, in any scope — a run's ``shell:`` steps and its
  agents execute with the user's own ``$HOME`` and inside the repository the worktree came from,
  so ``git config [--global] user.email …`` is one command in one step. And any **``.env`` file
  rayspec loaded** — ``$RAYSPEC_HOME/.env`` (``$RAYSPEC_HOME`` is exported into every step) and
  ``<project>/.rayspec/.env`` (a file in the tree the run works in) are both one ``printf`` away
  from a step, and both are copied straight into ``os.environ``. A file the audited run can
  write cannot say who audited it;
* nothing here opens a socket: every answer is available on the machine the run happens on;
* it never raises — a source that cannot answer simply falls through to the next one.

Somebody who wants a per-project or per-person identity sets :data:`ACTOR_ENV` in the shell that
launches the run or answers the gate. That is a decision taken outside the run, which is exactly
what makes it worth recording. Setting it in a ``.env`` instead does not fail silently: the
value is refused as the identity and kept as :attr:`ActorInfo.declared_id` — a claim a file on
this machine made — so the ledger shows what was asked for and what was recorded instead.

What this does NOT do: the run store is user-owned by design, so nothing here survives somebody
editing ``run.json`` afterwards, and ``rayspec audit`` says as much. The guarantee is narrower
and worth stating exactly — *at the moment a decision is recorded, the identity on it came from
the operator, not from the run.*

The record shape (:class:`~rayspec.store.model.ActorInfo`) lives with the rest of ``run.json``;
this module only fills it in.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Mapping

from rayspec.procenv import env_file_value, operator_env
from rayspec.store.model import ActorInfo
from rayspec.textsafe import safe_text

#: Environment variable that overrides the resolved identity (a scheduler sets it).
ACTOR_ENV = "RAYSPEC_ACTOR"
#: Longest identity recorded; anything longer is truncated with an ellipsis.
MAX_ACTOR_LEN = 256

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
    """The CI system this process runs under (``github-actions``, …), ``None`` on a laptop.

    Defaults to :func:`rayspec.procenv.operator_env`, like everything else in this module: a
    ``GITHUB_ACTIONS`` a run planted in a ``.env`` would otherwise make a laptop read as CI.
    """
    env = operator_env() if env is None else env
    for name, label in CI_ENV_MARKERS:
        if _truthy(env, name):
            return label
    return None


def provider_accounts(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``{provider id: account}`` for the accounts the environment NAMES (never a credential).

    Defaults to :func:`rayspec.procenv.operator_env`: an account a run wrote into a ``.env`` is
    the run naming somebody, and this record is read as a fact about who acted.
    """
    env = operator_env() if env is None else env
    out: dict[str, str] = {}
    for provider, names in PROVIDER_ACCOUNT_ENV.items():
        for name in names:
            account = clean_identity(env.get(name))
            if account is not None:
                out[provider] = account
                break
    return out


def os_user(env: Mapping[str, str] | None = None) -> str | None:
    """The operating-system user: the account database first, the usual variables after.

    :func:`getpass.getuser` consults ``LOGNAME``/``USER``/… *before* the account database, so it
    answers with whatever the caller's environment says. That is fine as a fallback and wrong as
    a first choice: ``source: "os"`` should mean the operating system said so. The user database
    is asked first where it exists (POSIX), and only a platform without it — or a uid with no
    entry, as in a container run under a random uid — falls through to the environment.

    Never raises: every lookup that can fail is a lookup that may return ``None``.

    ``env`` defaults to :func:`rayspec.procenv.operator_env` so the ``USER``/``LOGNAME`` fallback
    cannot be answered by a ``.env`` a run wrote either.
    """
    env = operator_env() if env is None else env
    try:
        import pwd  # POSIX only, and needed nowhere else

        name = clean_identity(pwd.getpwuid(os.getuid()).pw_name)
    except (ImportError, AttributeError, KeyError, OSError):  # no pwd (Windows), no entry
        name = None
    if name is None:
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
    """Who is running this — :data:`ACTOR_ENV`, else the OS user, else ``"unknown"``.

    ``env`` defaults to :func:`rayspec.procenv.operator_env`: the process environment with every
    variable rayspec copied out of a ``.env`` file taken back out again. **The whole record is
    resolved from it**, not only :data:`ACTOR_ENV` — a planted ``GITHUB_ACTIONS`` would forge
    ``ci`` and a planted ``ANTHROPIC_ACCOUNT`` would forge an account, and a ledger field that a
    run can choose is not worth more than a field it cannot. A git ``user.email`` is not read
    either, in any scope. There is no parameter for a directory, because the only directories on
    offer at the call sites are ones the run itself had write access to.

    ``declared_id`` is the one place a ``.env`` still shows up: when one supplied
    :data:`ACTOR_ENV`, that value is recorded as a *claim*, next to — never instead of — the
    identity that was actually used. Refusing it silently would leave a person who put
    ``RAYSPEC_ACTOR`` in a ``.env`` on purpose wondering why nothing happened.

    It is an identity for a log, not an authorisation: nothing in rayspec grants a permission
    because of it.
    """
    if env is None:
        # only the process environment can hold a value rayspec put there
        declared = clean_identity(env_file_value(ACTOR_ENV))
        env = operator_env()
    else:
        declared = None  # the caller says this mapping IS the operator's environment
    identity = clean_identity(env.get(ACTOR_ENV))
    source = "env"
    if identity is None:
        identity, source = os_user(env), "os"
    if identity is None:
        identity, source = "unknown", "unknown"
    return ActorInfo(
        id=identity,
        source=source,
        ci=detect_ci(env),
        provider_accounts=provider_accounts(env),
        declared_id=declared,
    )


__all__ = [
    "ACTOR_ENV",
    "CI_ENV_MARKERS",
    "MAX_ACTOR_LEN",
    "PROVIDER_ACCOUNT_ENV",
    "clean_identity",
    "detect_ci",
    "os_user",
    "provider_accounts",
    "resolve_actor",
]
