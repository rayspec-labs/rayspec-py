# SPDX-License-Identifier: Apache-2.0
"""Configuration: ``RAYSPEC_HOME``, ``config.yaml`` (user + project merge) and ``.env`` loading.

Boundary: this package knows about files under ``~/.rayspec`` and ``<project>/.rayspec`` and
nothing about workflows. It depends on :mod:`rayspec.schema` (for ``StrictModel``) and
:mod:`rayspec.loader.yaml` only.
"""

from rayspec.config.model import (
    DEFAULT_TIERS,
    DETECTOR_NAMES,
    TIER_NAMES,
    AliasSpec,
    Config,
    ExtensionsSpec,
    ProjectSpec,
    RedactSpec,
    SecretSourceSpec,
    TierSpec,
)
from rayspec.config.paths import rayspec_home
from rayspec.config.settings import (
    ConfigError,
    ProjectEnvInfo,
    config_layers,
    env_paths,
    load_config,
    load_env,
    merge_config_data,
    parse_env_text,
    project_env_info,
)

__all__ = [
    "DEFAULT_TIERS",
    "DETECTOR_NAMES",
    "TIER_NAMES",
    "AliasSpec",
    "Config",
    "ConfigError",
    "ExtensionsSpec",
    "ProjectEnvInfo",
    "ProjectSpec",
    "RedactSpec",
    "SecretSourceSpec",
    "TierSpec",
    "config_layers",
    "env_paths",
    "load_config",
    "load_env",
    "merge_config_data",
    "parse_env_text",
    "project_env_info",
    "rayspec_home",
]
