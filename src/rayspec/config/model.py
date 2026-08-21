# SPDX-License-Identifier: Apache-2.0
"""The ``Config`` pydantic model (``config.yaml``) and built-in model tiers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, field_validator, model_validator

from rayspec.schema import StrictModel
from rayspec.schema.common import EffortName

#: Model tiers an agent may name instead of a literal model id.
TIER_NAMES: tuple[str, ...] = ("small", "medium", "large")


class TierSpec(StrictModel):
    """A tier entry: ``model`` plus an optional default ``effort``."""

    model: str
    effort: EffortName | None = None

    @classmethod
    def _what(cls) -> str:
        return "tier"


def _tier_from_str(value: Any) -> Any:
    if isinstance(value, str):
        return {"model": value}
    return value


TierValue = Annotated[TierSpec, BeforeValidator(_tier_from_str)]


class AliasSpec(StrictModel):
    """An ``@alias`` usable as ``model:``; may also pin the provider and effort."""

    provider: str | None = None
    model: str
    effort: EffortName | None = None

    @classmethod
    def _what(cls) -> str:
        return "alias"


class SecretSourceSpec(StrictModel):
    """Where one entry of the ``secrets:`` block gets its value from.

    Exactly one of ``env`` (an environment variable), ``file`` (a path, refused unless the file
    is mode ``0600`` or tighter) or ``cmd`` (a command whose stdout is the value) must be given.
    ``required: false`` makes a missing value simply absent instead of an error. The value is
    resolved once at run start, handed only to ``shell:``/``python:`` steps as the environment
    variable ``<NAME>``, redacted from every writer — and never persisted.
    """

    env: str | None = None
    file: str | None = None
    cmd: str | list[str] | None = None
    required: bool = True

    @classmethod
    def _what(cls) -> str:
        return "secret source"

    @model_validator(mode="after")
    def _exactly_one_source(self) -> SecretSourceSpec:
        given = [n for n in ("env", "file", "cmd") if getattr(self, n) is not None]
        if len(given) != 1:
            listed = ", ".join(given) if given else "none"
            raise ValueError(f"a secret source needs exactly one of env, file, cmd (got: {listed})")
        if isinstance(self.cmd, list) and not self.cmd:
            raise ValueError("secret source cmd must not be an empty list")
        return self

    @property
    def kind(self) -> str:
        """``"env"`` · ``"file"`` · ``"cmd"`` — which source this spec names."""
        return "env" if self.env is not None else "file" if self.file is not None else "cmd"


#: Environment variables a ``secrets:`` entry may not be named after: replacing one of them
#: breaks the step in a way that never points back at the secret.
RESERVED_SECRET_NAMES: frozenset[str] = frozenset(
    {"PATH", "HOME", "PWD", "SHELL", "IFS", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH"}
)

#: A usable environment variable name (the form a ``secrets:`` key must have).
_SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Built-in opt-in redaction detectors (``config.redact.detectors``); see ``rayspec.redact``.
DETECTOR_NAMES: tuple[str, ...] = ("github", "openai", "aws", "jwt", "pem")


class RedactSpec(StrictModel):
    """The ``redact:`` block: which builtin detectors the Redactor adds on top of the known
    secret values.

    Known values (declared ``secret: true`` inputs and every ``secrets:`` entry) are ALWAYS
    redacted. The pattern detectors are opt-in and default to off: a false positive in a run log
    is worse than the gap.
    """

    detectors: list[str] = Field(default_factory=list)

    @classmethod
    def _what(cls) -> str:
        return "redact"

    @field_validator("detectors")
    @classmethod
    def _known_detectors(cls, value: list[str]) -> list[str]:
        for name in value:
            if name not in DETECTOR_NAMES and name != "all":
                raise ValueError(
                    f"unknown redact detector {name!r}; known: {', '.join(DETECTOR_NAMES)}, all"
                )
        return value

    def resolved_detectors(self) -> tuple[str, ...]:
        """The detector names to build, expanding ``all``."""
        if "all" in self.detectors:
            return DETECTOR_NAMES
        return tuple(dict.fromkeys(self.detectors))


class ExtensionsSpec(StrictModel):
    """The ``extensions:`` block: which registered sinks and approval prompt a run uses.

    Ids are resolved through :mod:`rayspec.registry`, which knows the builtins and everything
    installed packages publish under the ``rayspec.sinks`` / ``rayspec.approvals`` entry-point
    groups (``rayspec plugins`` lists them). Both keys are optional and default to what rayspec
    has always done: the console (or ``--json``) sink the CLI flags pick, and the interactive
    terminal approval prompt. ``sinks`` are ADDITIONAL observers — they join the CLI's own sink
    rather than replacing it, and they are redacted like every other sink.
    """

    sinks: list[str] = Field(default_factory=list)
    approval: str | None = None
    #: extension id → the settings mapping its factory is handed (``providers:``-shaped).
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @classmethod
    def _what(cls) -> str:
        return "extensions"

    def settings_for(self, extension_id: str) -> dict[str, Any]:
        """The settings mapping of one extension id (empty when it has none)."""
        return dict(self.settings.get(extension_id, {}))


class ProjectSpec(StrictModel):
    """A registered project (``rayspec projects add``)."""

    name: str
    source: str
    base: str | None = None

    @classmethod
    def _what(cls) -> str:
        return "project"


def _tiers(**specs: TierSpec) -> Mapping[str, TierSpec]:
    return MappingProxyType(dict(specs))


#: Built-in tiers used when ``config.tiers`` does not define one for a provider (read-only).
DEFAULT_TIERS: Mapping[str, Mapping[str, TierSpec]] = MappingProxyType(
    {
        "claude": _tiers(
            small=TierSpec(model="haiku"),
            medium=TierSpec(model="sonnet"),
            large=TierSpec(model="opus"),
        ),
        "codex": _tiers(
            small=TierSpec(model="gpt-5.4", effort="low"),
            medium=TierSpec(model="gpt-5.4"),
            large=TierSpec(model="gpt-5.4", effort="high"),
        ),
        "stub": _tiers(
            small=TierSpec(model="stub-small"),
            medium=TierSpec(model="stub-medium"),
            large=TierSpec(model="stub-large"),
        ),
    }
)


class Config(StrictModel):
    """Merged ``~/.rayspec/config.yaml`` + ``.rayspec/config.yaml``."""

    default_provider: str = "claude"
    tiers: dict[str, dict[str, TierValue]] = Field(default_factory=dict)
    aliases: dict[str, AliasSpec] = Field(default_factory=dict)
    pricing: dict[str, Any] = Field(default_factory=dict)
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    projects: list[ProjectSpec] = Field(default_factory=list)
    #: NAME → where its value comes from (env/file/cmd), resolved lazily at run start.
    secrets: dict[str, SecretSourceSpec] = Field(default_factory=dict)
    #: opt-in builtin redaction detectors on top of the known secret values.
    redact: RedactSpec = Field(default_factory=RedactSpec)
    #: which registered sinks and approval prompt a run uses (see :class:`ExtensionsSpec`).
    extensions: ExtensionsSpec = Field(default_factory=ExtensionsSpec)

    @classmethod
    def _what(cls) -> str:
        return "config"

    @field_validator("secrets")
    @classmethod
    def _usable_secret_names(
        cls, value: dict[str, SecretSourceSpec]
    ) -> dict[str, SecretSourceSpec]:
        """A secret name becomes an environment variable of every ``shell:``/``python:`` step.

        A name that is not a valid variable name could never be read, and one that shadows a
        variable the engine sets itself silently rewrites the step's environment — a ``PATH``
        entry made a plain ``echo`` step fail with ``No such file or directory: 'bash'`` and
        nothing pointed at the secret. Both are refused here, where the message
        can still name the offending key.
        """
        for name in value:
            if not _SECRET_NAME.fullmatch(name):
                raise ValueError(
                    f"secret name {name!r} is not a usable environment variable name "
                    "(letters, digits and _, not starting with a digit)"
                )
            if name.startswith("RAYSPEC_"):
                raise ValueError(
                    f"secret name {name!r} is reserved: RAYSPEC_* variables are set by the "
                    "engine itself (RAYSPEC_CONTEXT, RAYSPEC_STEP_PATH, RAYSPEC_INPUT_*)"
                )
            if name in RESERVED_SECRET_NAMES:
                raise ValueError(
                    f"secret name {name!r} would replace the step's own {name} variable; "
                    "give the secret its own name and read it from there"
                )
        return value

    @field_validator("tiers")
    @classmethod
    def _tier_names(cls, value: dict[str, dict[str, TierSpec]]) -> dict[str, dict[str, TierSpec]]:
        for provider, tiers in value.items():
            for tier in tiers:
                if tier not in TIER_NAMES:
                    raise ValueError(
                        f"unknown tier {tier!r} for provider {provider!r}; "
                        f"tiers are {', '.join(TIER_NAMES)}"
                    )
        return value

    @field_validator("aliases")
    @classmethod
    def _alias_names(cls, value: dict[str, AliasSpec]) -> dict[str, AliasSpec]:
        for name in value:
            if not name.startswith("@") or len(name) < 2:
                raise ValueError(f"alias {name!r} must start with '@' (e.g. '@mini')")
        return value

    def resolve_tier(self, provider: str, tier: str) -> TierSpec | None:
        """Look up ``tier`` for ``provider`` (config first, then the built-in defaults)."""
        spec = self.tiers.get(provider, {}).get(tier)
        if spec is not None:
            return spec
        return DEFAULT_TIERS.get(provider, {}).get(tier)


__all__ = [
    "DEFAULT_TIERS",
    "DETECTOR_NAMES",
    "RESERVED_SECRET_NAMES",
    "TIER_NAMES",
    "AliasSpec",
    "Config",
    "ProjectSpec",
    "RedactSpec",
    "SecretSourceSpec",
    "TierSpec",
]
