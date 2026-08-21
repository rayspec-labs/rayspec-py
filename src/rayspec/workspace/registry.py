# SPDX-License-Identifier: Apache-2.0
"""Registered projects (``config.projects`` in ``<home>/config.yaml``): add / list / remove.

Boundary: the only writer of the user ``config.yaml``. Other keys are preserved verbatim
(comments are not — the file is rewritten with ``yaml.safe_dump``). Reading uses the same YAML
reader as the config loader (``rayspec.loader.yaml.load_yaml``, imported lazily — the
one place the workspace layer reaches into ``loader/``) so validation matches
``rayspec.config.load_config``. The file is written private (``0600``) into a private home
(``0700``, the store's ``secure_mkdir``); every write goes through a fresh ``mkstemp`` temp
file (``0600`` by construction, never a pre-existing — possibly world-readable — ``*.tmp``) +
``os.replace`` (like ``run.json``), so a rewrite leaves the file ``0600`` too.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from rayspec.config import Config, ProjectSpec
from rayspec.errors import LoaderError, RayspecError
from rayspec.store.file import secure_mkdir
from rayspec.workspace.errors import WorkspaceError
from rayspec.workspace.repos import is_git_url

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def user_config_path(home: Path) -> Path:
    """``<home>/config.yaml``."""
    return home / "config.yaml"


def _read_raw(home: Path) -> dict[str, Any]:
    path = user_config_path(home)
    if not path.is_file():
        return {}
    from rayspec.loader.yaml import load_yaml  # lazy: rayspec.loader is a heavier import

    data = load_yaml(path.read_text(encoding="utf-8"), source=str(path))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise LoaderError(f"{path}: config must be a mapping, got {type(data).__name__}")
    return data


def _write_raw(home: Path, data: dict[str, Any]) -> None:
    path = user_config_path(home)
    secure_mkdir(path.parent)  # a fresh $RAYSPEC_HOME is 0700
    text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
    # A fresh private temp file every time (``mkstemp``: 0600 by construction, unique name, never
    # an existing file — a stale world-readable ``config.yaml.tmp`` must not be reused) +
    # ``os.replace``: the installed file is 0600 regardless of umask or what was there before.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _validate(data: dict[str, Any], *, source: str) -> Config:
    try:
        return Config.parse(data, source=source)
    except RayspecError as exc:  # SchemaError from Config.parse → the loader's error type
        raise LoaderError(str(exc)) from None


def list_projects(home: Path) -> list[ProjectSpec]:
    """Registered projects from the user config, sorted by name."""
    data = _read_raw(home)
    config = _validate(data, source=str(user_config_path(home)))
    return sorted(config.projects, key=lambda p: p.name)


def validate_project_name(name: str) -> str:
    """A project name: letters, digits, ``.``, ``_``, ``-`` (no spaces or slashes)."""
    if not _NAME_RE.match(name):
        raise WorkspaceError(
            f"invalid project name {name!r}",
            hint="use letters, digits, '.', '_' or '-' (e.g. myapp)",
        )
    return name


def normalize_source(source: str, *, cwd: Path | None = None) -> str:
    """A git URL is kept as-is; a local path must exist and is stored absolute."""
    text = source.strip()
    if not text:
        raise WorkspaceError("project source must not be empty")
    if is_git_url(text):
        return text
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() if cwd is None else cwd) / path
    path = path.resolve()
    if not path.is_dir():
        raise WorkspaceError(
            f"source {source!r}: {path} is not a directory",
            hint="pass an existing local checkout or a git URL",
        )
    return str(path)


def add_project(
    home: Path, name: str, source: str, *, base: str | None, cwd: Path | None = None
) -> bool:
    """Register (or update) ``name`` → ``source``; returns True when it replaced an entry."""
    validate_project_name(name)
    source = normalize_source(source, cwd=cwd)
    data = _read_raw(home)
    entries = list(data.get("projects") or [])
    entry: dict[str, Any] = {"name": name, "source": source}
    if base:
        entry["base"] = base
    replaced = False
    for i, existing in enumerate(entries):
        if isinstance(existing, dict) and existing.get("name") == name:
            entries[i] = entry
            replaced = True
            break
    else:
        entries.append(entry)
    data["projects"] = entries
    _validate(data, source=str(user_config_path(home)))
    _write_raw(home, data)
    return replaced


def remove_project(home: Path, name: str) -> bool:
    """Unregister ``name``; returns False when no such project was registered."""
    data = _read_raw(home)
    entries = list(data.get("projects") or [])
    kept = [e for e in entries if not (isinstance(e, dict) and e.get("name") == name)]
    if len(kept) == len(entries):
        return False
    data["projects"] = kept
    _write_raw(home, data)
    return True


__all__ = [
    "add_project",
    "list_projects",
    "normalize_source",
    "remove_project",
    "user_config_path",
    "validate_project_name",
]
