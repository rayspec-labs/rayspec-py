# SPDX-License-Identifier: Apache-2.0
"""Secret *sources* — where a secret value comes from.

Boundary: this package resolves names to values and describes the configured sources. It never
persists, prints or logs a value; keeping them out of the run store is
:mod:`rayspec.redact`'s job and delivering them to a step is the engine's.

The one entry point the CLI uses is :func:`resolve_config_secrets` (every ``config.secrets``
entry, resolved once at run start) plus :class:`ConfigSecretProvider` (the same table as a lazy
:class:`~rayspec.secrets.base.SecretProvider`, used by ``resume``/``approve``/``reject`` to
re-fetch a secret input instead of asking for it again).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from rayspec.config.model import Config, SecretSourceSpec
from rayspec.redact import Redactor
from rayspec.secrets.base import SecretError, SecretProvider
from rayspec.secrets.sources import (
    CMD_TIMEOUT_S,
    describe_sources,
    resolve_source,
    secret_file_path,
)


class ConfigSecretProvider:
    """A :class:`SecretProvider` over a ``config.secrets`` table.

    Resolution is lazy and memoised: a name is read from its source at most once per process, so
    a ``cmd:`` source (``op read``, ``pass``, a keychain helper) is not re-run for every lookup.
    A name the table does not know is ``None``; a known name that cannot be resolved raises
    :class:`SecretError` — except an optional one (``required: false``), which is ``None`` too.
    """

    def __init__(
        self,
        specs: Mapping[str, SecretSourceSpec],
        *,
        env: Mapping[str, str] | None = None,
        base_dir: Path | None = None,
    ) -> None:
        self._specs = dict(specs)
        self._env = os.environ if env is None else env
        self._base_dir = Path.cwd() if base_dir is None else Path(base_dir)
        self._cache: dict[str, str | None] = {}

    def names(self) -> tuple[str, ...]:
        """Every configured secret name, in declaration order."""
        return tuple(self._specs)

    def get(self, name: str) -> str | None:
        """Resolve ``name`` (memoised); ``None`` when it is not configured or optional-absent."""
        if name in self._cache:
            return self._cache[name]
        spec = self._specs.get(name)
        if spec is None:
            return None
        value = resolve_source(name, spec, env=self._env, base_dir=self._base_dir)
        self._cache[name] = value
        return value

    def resolve_all(self) -> dict[str, str]:
        """Every configured secret that has a value (optional-absent ones are simply missing)."""
        out: dict[str, str] = {}
        for name in self._specs:
            value = self.get(name)
            if value is not None:
                out[name] = value
        return out

    def describe(self) -> tuple[tuple[str, str], ...]:
        """``((name, source), …)`` — the sources, never the values."""
        return describe_sources(self._specs)


def provider_for(
    config: Config, *, env: Mapping[str, str] | None = None, base_dir: Path | None = None
) -> ConfigSecretProvider:
    """The :class:`ConfigSecretProvider` for ``config.secrets``."""
    return ConfigSecretProvider(config.secrets, env=env, base_dir=base_dir)


def resolve_config_secrets(
    config: Config, *, env: Mapping[str, str] | None = None, base_dir: Path | None = None
) -> dict[str, str]:
    """Resolve every ``config.secrets`` entry once (``NAME`` → value).

    Raises :class:`SecretError` naming the first entry that cannot be resolved.
    """
    return provider_for(config, env=env, base_dir=base_dir).resolve_all()


def secret_input_overlay(
    provider: SecretProvider,
    names: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
    problems: list[str] | None = None,
) -> dict[str, str]:
    """``{RAYSPEC_INPUT_<NAME>: value}`` for every secret input the provider can supply.

    Handed to :func:`~rayspec.loader.inputs.resolve_inputs` /
    :func:`~rayspec.loader.inputs.resolve_resume_secrets` as an environment overlay, which makes
    the precedence ``--input``/``--inputs-file`` > ``config.secrets`` > ``RAYSPEC_INPUT_<NAME>``
    > ``default`` without either function having to know about secret sources. This is what lets
    ``resume``/``approve``/``reject`` continue a paused run without re-typing its secrets.

    With ``problems`` given, a :class:`SecretError` is APPENDED to that list instead of raised:
    the caller has another way to obtain the value (``--input``) and a source that is briefly
    unavailable must not strand a paused run. The caller reports the collected messages only if
    the name is still missing afterwards.
    """
    from rayspec.loader.inputs import env_var_name

    base = dict(os.environ if env is None else env)
    for name in names:
        try:
            value = provider.get(name)
        except SecretError as exc:
            if problems is None:
                raise
            problems.append(str(exc))
            continue
        if value is not None:
            base[env_var_name(name)] = value
    return base


def used_config_secrets(
    provider: SecretProvider, steps: Iterable[Any], names: Iterable[str]
) -> dict[str, str]:
    """The ``config.secrets`` entries this run can actually read, resolved (``NAME`` → value).

    Resolution is LAZY: only the names a ``shell:``/``python:`` step of the
    workflow mentions are read from their source, so an unused (or stale) entry in
    ``~/.rayspec/config.yaml`` neither fails a run nor runs its ``cmd:`` helper. An entry the
    run *does* need still raises :class:`SecretError` — that failure is the point.

    See :func:`rayspec.loader.secrets.config_secrets_in_use` for what "in use" means.
    """
    from rayspec.loader.secrets import config_secrets_in_use

    out: dict[str, str] = {}
    for name in config_secrets_in_use(steps, names):
        value = provider.get(name)
        if value is not None:
            out[name] = value
    return out


def build_redactor(
    config: Config, secrets: Mapping[str, Any], *, identities: Iterable[str] = ()
) -> Redactor:
    """The run's :class:`~rayspec.redact.Redactor`: every known value plus the opt-in detectors.

    ``secrets`` is ``{name: value}`` over the declared ``secret: true`` inputs that were given
    **and** every resolved ``config.secrets`` entry — the two sets of values rayspec knows and
    must therefore keep out of the store, the logs and the console.

    ``identities`` are the strings the run is recorded UNDER
    (:func:`rayspec.store.model.identity_strings`). A secret whose value equals one of them is
    not redacted anywhere — see :meth:`rayspec.redact.Redactor.build` for why hiding it in some
    places and not others is worse than not hiding it at all — and is named in
    :attr:`~rayspec.redact.Redactor.collisions` so the caller can say so.
    """
    return Redactor.build(
        secrets, detectors=config.redact.resolved_detectors(), identities=identities
    )


__all__ = [
    "CMD_TIMEOUT_S",
    "ConfigSecretProvider",
    "SecretError",
    "SecretProvider",
    "build_redactor",
    "describe_sources",
    "provider_for",
    "resolve_config_secrets",
    "resolve_source",
    "secret_file_path",
    "secret_input_overlay",
    "used_config_secrets",
]
