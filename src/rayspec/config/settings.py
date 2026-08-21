# SPDX-License-Identifier: Apache-2.0
"""Loading and merging ``config.yaml`` files and ``.env`` files.

Every problem with these files — YAML syntax, an unsafe tag, a non-mapping document, a wrong
field type, an unreadable file — is raised as one :class:`ConfigError` (a
:class:`~rayspec.errors.LoaderError`) whose message starts with ``<path>[:<line>]:`` so the CLI
boundary can print ``error: <path>:<line>: …`` and exit 2.

``.env`` trust: ``<home>/.env`` is the user's own file and is always loaded;
``<project>/.rayspec/.env`` belongs to whoever pushed the checkout and is applied only when the
caller passes ``include_project=True`` (the execution commands ``run``/``resume``/``approve``/
``reject``) — inspection commands never see it.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rayspec.config.model import Config
from rayspec.config.paths import rayspec_home
from rayspec.errors import LoaderError
from rayspec.schema import SchemaError


class ConfigError(LoaderError):
    """A ``config.yaml`` or ``.env`` file cannot be read, parsed or validated.

    The message names the file (``<path>:<line>: …`` when a line is known); ``hint`` may carry
    the YAML loader's suggestion. The CLI catches it at the command boundary (exit 2).
    """


#: Top-level keys whose mappings are merged per key (one level for aliases, two for tiers).
_MERGE_DEPTH: dict[str, int] = {"tiers": 2, "aliases": 1, "secrets": 1, "redact": 1}


def _merge_dicts(base: dict[str, Any], override: dict[str, Any], depth: int) -> dict[str, Any]:
    if depth <= 0:
        return dict(override)
    out = dict(base)
    for key, value in override.items():
        if depth > 1 and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_dicts(out[key], value, depth - 1)
        else:
            out[key] = value
    return out


def merge_config_data(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge raw config mappings: shallow per top-level key, ``tiers``/``aliases`` per key."""
    out = dict(base)
    for key, value in override.items():
        depth = _MERGE_DEPTH.get(key, 0)
        if depth and isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_dicts(out[key], value, depth)
        else:
            out[key] = value
    return out


def _read_text(path: Path, *, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: {what} is not UTF-8: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read {what}: {exc.strerror or exc}") from None


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    # lazy: rayspec.loader imports rayspec.config (paths) — avoid an import cycle at module load
    from rayspec.loader.yaml import load_yaml

    text = _read_text(path, what="config")
    try:
        data = load_yaml(text, source=str(path))
    except LoaderError as exc:
        raise ConfigError(str(exc), hint=exc.hint, location=exc.location) from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: config must be a mapping, got {type(data).__name__}")
    return data


def config_paths(project_root: Path, home: Path) -> tuple[Path, Path]:
    """``(user_config, project_config)`` paths for the given roots."""
    return home / "config.yaml", project_root / ".rayspec" / "config.yaml"


def load_config(project_root: Path | None, *, home: Path | None = None) -> Config:
    """Load ``<home>/config.yaml`` then ``<project_root>/.rayspec/config.yaml`` (project wins).

    Raises :class:`ConfigError` (never a raw YAML/pydantic exception) naming the offending file.
    """
    home = rayspec_home() if home is None else home
    root = Path.cwd() if project_root is None else project_root
    user_path, project_path = config_paths(root, home)
    layers: list[dict[str, Any]] = []
    for path in (user_path, project_path):
        data = _read_config_file(path)
        if data:
            _validate(data, source=str(path))  # report errors against the file that has them
        layers.append(data)
    merged = merge_config_data(*layers)
    sources = " + ".join(
        str(p) for p, d in zip((user_path, project_path), layers, strict=True) if d
    )
    return _validate(merged, source=sources or str(project_path))


def _validate(data: dict[str, Any], *, source: str) -> Config:
    try:
        return Config.parse(data, source=source)
    except SchemaError as exc:
        raise ConfigError(str(exc)) from None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """Parse a simple ``KEY=VALUE`` file (``#`` comments, optional ``export``, quotes stripped)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        out[key] = _unquote(value)
    return out


def env_paths(project_root: Path, home: Path) -> tuple[Path, Path]:
    """``(user_env, project_env)`` paths for the given roots."""
    return home / ".env", project_root / ".rayspec" / ".env"


@dataclass(frozen=True, slots=True)
class ProjectEnvInfo:
    """The project ``.rayspec/.env`` file that exists at ``path`` and defines ``count`` vars."""

    path: Path
    count: int


def project_env_info(project_root: Path | None) -> ProjectEnvInfo | None:
    """Describe ``<project>/.rayspec/.env`` (``None`` when absent) — for notices and ``doctor``.

    Never raises: an unreadable file counts as ``0`` variables.
    """
    root = Path.cwd() if project_root is None else project_root
    path = root / ".rayspec" / ".env"
    if not path.is_file():
        return None
    try:
        count = len(parse_env_text(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError):
        count = 0
    return ProjectEnvInfo(path=path, count=count)


def load_env(
    project_root: Path | None,
    *,
    home: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    override: bool = False,
    include_project: bool = False,
) -> dict[str, str]:
    """Load ``<home>/.env`` — and, with ``include_project=True``, ``<project>/.rayspec/.env``
    (project wins) — into ``environ``.

    The project file is a credential surface controlled by whoever pushed the checkout
    (``ANTHROPIC_BASE_URL``, ``GIT_CONFIG_*`` …): only the execution commands opt in.
    Variables already present in ``environ`` are kept unless ``override`` is true. Returns the
    variables that were actually applied. Raises :class:`ConfigError` for an unreadable file.
    """
    home = rayspec_home() if home is None else home
    root = Path.cwd() if project_root is None else project_root
    target: MutableMapping[str, str] = os.environ if environ is None else environ
    user_path, project_path = env_paths(root, home)
    paths = [user_path, project_path] if include_project else [user_path]
    values: dict[str, str] = {}
    for path in paths:
        if path.exists():
            values.update(parse_env_text(_read_text(path, what=".env")))
    applied: dict[str, str] = {}
    for key, value in values.items():
        if override or key not in target:
            target[key] = value
            applied[key] = value
    return applied


__all__ = [
    "ConfigError",
    "ProjectEnvInfo",
    "config_paths",
    "env_paths",
    "load_config",
    "load_env",
    "merge_config_data",
    "parse_env_text",
    "project_env_info",
]
