# SPDX-License-Identifier: Apache-2.0
"""Third-party CLI commands: discovery of the ``rayspec.cli_plugins`` entry-point group.

Module boundary: this module knows how to turn installed entry points into commands on a
:class:`typer.Typer` app and how to describe what it found. It owns no commands itself and
never decides *what* a command does.

A plugin exposes exactly what a builtin command module exposes — a callable
``register(app: typer.Typer) -> None`` — so a third-party command module is literally the same
code as ``rayspec/cli/commands/<name>.py``::

    [project.entry-points."rayspec.cli_plugins"]
    acme = "acme_rayspec.cli:register"

The rules mirror :mod:`rayspec.providers.registry` and are fixed:

* builtin commands are registered first and can never be shadowed — a plugin command whose name
  is already taken is removed again and reported with a :class:`RuntimeWarning`;
* plugins are visited in entry-point name order, so a collision between two plugins resolves
  the same way on every machine;
* a plugin that fails to import, is not callable, or raises while registering is skipped with a
  :class:`RuntimeWarning` and anything it managed to add is rolled back — ``rayspec --help``
  keeps working with a broken plugin installed;
* the root callback belongs to rayspec: a plugin that replaces it has the replacement dropped.

Cost: when nothing is installed under the group, no plugin module is imported at all — the scan
is one metadata query and the CLI starts as before.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Any

import typer

#: Entry-point group scanned for third-party commands (value = ``module:register``).
CLI_ENTRY_POINT_GROUP = "rayspec.cli_plugins"

#: Entry-point groups a rayspec plugin can publish, in the order ``rayspec plugins`` lists them.
PLUGIN_GROUPS: tuple[str, ...] = (
    CLI_ENTRY_POINT_GROUP,
    "rayspec.providers",
    "rayspec.stores",
    "rayspec.sinks",
    "rayspec.approvals",
)


@dataclass(frozen=True)
class LoadedCliPlugin:
    """One visited ``rayspec.cli_plugins`` entry point and what it contributed."""

    name: str
    value: str
    distribution: str | None = None
    version: str | None = None
    commands: tuple[str, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the plugin loaded and at least nothing was refused."""
        return self.error is None


@dataclass
class _State:
    """What the last :func:`register_cli_plugins` call found (for ``rayspec plugins``)."""

    loaded: tuple[LoadedCliPlugin, ...] = ()
    scanned: bool = False
    problems: list[str] = field(default_factory=list)


_state = _State()


def reset_cli_plugins() -> None:
    """Forget the recorded discovery result. Intended for tests."""
    _state.loaded = ()
    _state.scanned = False
    _state.problems.clear()


def loaded_cli_plugins() -> tuple[LoadedCliPlugin, ...]:
    """The plugins the last :func:`register_cli_plugins` call visited (newest call wins)."""
    return _state.loaded


def command_names(app: typer.Typer) -> set[str]:
    """Every command and sub-command group name currently registered on ``app``."""
    names = {_command_name(info) for info in app.registered_commands}
    names |= {_group_name(info) for info in app.registered_groups}
    names.discard("")
    return names


def _command_name(info: Any) -> str:
    """The name Typer will expose a registered command under (its own rule, applied early)."""
    from typer.main import get_command_name

    name = getattr(info, "name", None)
    if name:
        return str(name)
    callback = getattr(info, "callback", None)
    return get_command_name(callback.__name__) if callback is not None else ""


def _group_name(info: Any) -> str:
    """The name of an ``add_typer`` sub-app (its own ``name=`` or the sub-app's)."""
    name = getattr(info, "name", None)
    if name:
        return str(name)
    sub = getattr(info, "typer_instance", None)
    sub_name = getattr(getattr(sub, "info", None), "name", None)
    if sub_name:
        return str(sub_name)
    return _command_name(info)


def _describe(ep: EntryPoint) -> tuple[str | None, str | None]:
    """``(distribution name, version)`` behind an entry point, as far as metadata knows."""
    dist = getattr(ep, "dist", None)
    if dist is None:
        return None, None
    return getattr(dist, "name", None), getattr(dist, "version", None)


def _entry_points(group: str) -> list[EntryPoint]:
    """Installed entry points of ``group``, sorted by name (never raises)."""
    try:
        return sorted(entry_points(group=group), key=lambda ep: ep.name)
    except Exception as exc:  # pragma: no cover - metadata backends are exotic
        warnings.warn(
            f"rayspec: cannot scan entry points {group!r}: {exc}", RuntimeWarning, stacklevel=2
        )
        return []


def _warn(message: str) -> None:
    warnings.warn(f"rayspec: {message}", RuntimeWarning, stacklevel=3)


def register_cli_plugins(app: typer.Typer) -> tuple[LoadedCliPlugin, ...]:
    """Register every installed CLI plugin on ``app`` (builtins must already be registered).

    Returns one :class:`LoadedCliPlugin` per visited entry point — the record ``rayspec plugins``
    prints. Never raises: every failure is a :class:`RuntimeWarning` and a skipped plugin.
    """
    eps = _entry_points(CLI_ENTRY_POINT_GROUP)
    _state.scanned = True
    if not eps:
        _state.loaded = ()
        return ()
    taken = command_names(app)
    loaded = [_register_one(app, ep, taken) for ep in eps]
    _state.loaded = tuple(loaded)
    return _state.loaded


def _register_one(app: typer.Typer, ep: EntryPoint, taken: set[str]) -> LoadedCliPlugin:
    """Load one entry point and let it register commands; roll back anything it breaks."""
    distribution, version = _describe(ep)
    where = f"CLI plugin {ep.name!r} ({ep.value})"

    def failed(error: str) -> LoadedCliPlugin:
        _warn(f"{where} {error}; skipped")
        return LoadedCliPlugin(ep.name, ep.value, distribution, version, error=error)

    try:
        target = ep.load()
    except Exception as exc:
        return failed(f"failed to load: {type(exc).__name__}: {exc}")
    if not callable(target):
        return failed(f"is not callable (got {type(target).__name__})")

    commands_before = len(app.registered_commands)
    groups_before = len(app.registered_groups)
    callback_before = app.registered_callback
    try:
        target(app)
    except Exception as exc:
        del app.registered_commands[commands_before:]
        del app.registered_groups[groups_before:]
        app.registered_callback = callback_before
        return failed(f"raised while registering: {type(exc).__name__}: {exc}")

    if app.registered_callback is not callback_before:
        app.registered_callback = callback_before
        _warn(f"{where} replaced the root callback; the replacement was dropped")

    kept: list[str] = []
    refused: list[str] = []
    app.registered_commands[:] = _keep_new(
        app.registered_commands,
        commands_before,
        _command_name,
        taken=taken,
        kept=kept,
        refused=refused,
    )
    app.registered_groups[:] = _keep_new(
        app.registered_groups, groups_before, _group_name, taken=taken, kept=kept, refused=refused
    )
    if refused:
        listed = ", ".join(repr(name) for name in refused)
        _warn(
            f"{where} tried to register the command {listed}, which rayspec already provides; "
            "a plugin can not shadow an existing command, so it was dropped"
        )
    error = "every command it registers is already taken" if refused and not kept else None
    return LoadedCliPlugin(ep.name, ep.value, distribution, version, tuple(kept), error)


def _keep_new(
    entries: list[Any],
    first_new: int,
    name_of: Callable[[Any], str],
    *,
    taken: set[str],
    kept: list[str],
    refused: list[str],
) -> list[Any]:
    """``entries`` without the newly added ones whose name is already ``taken``.

    Everything registered before ``first_new`` is kept untouched — that is the builtin surface,
    which a plugin may never displace.
    """
    out = list(entries[:first_new])
    for info in entries[first_new:]:
        name = name_of(info)
        if name in taken:
            refused.append(name)
            continue
        taken.add(name)
        kept.append(name)
        out.append(info)
    return out


@dataclass(frozen=True)
class InstalledPlugin:
    """One installed entry point across every rayspec plugin group (``rayspec plugins``)."""

    group: str
    name: str
    value: str
    distribution: str | None = None
    version: str | None = None
    status: str = "installed"
    detail: str = ""


def installed_plugins() -> list[InstalledPlugin]:
    """Every entry point installed under a rayspec plugin group, with its status.

    Nothing is imported here that is not already imported: the CLI groups report what
    :func:`register_cli_plugins` recorded, the extension groups what
    :mod:`rayspec.registry` resolved, and ``rayspec.providers`` is listed from metadata only
    (``rayspec providers`` is where a provider is inspected).
    """
    from rayspec import registry

    cli_by_name = {plugin.name: plugin for plugin in loaded_cli_plugins()}
    problems = {
        (problem.group, problem.name): problem.message for problem in registry.discovery_problems()
    }
    rows: list[InstalledPlugin] = []
    for group in PLUGIN_GROUPS:
        for ep in _entry_points(group):
            distribution, version = _describe(ep)
            status, detail = "installed", ""
            if group == CLI_ENTRY_POINT_GROUP:
                plugin = cli_by_name.get(ep.name)
                if plugin is None:
                    status, detail = "not scanned", ""
                elif plugin.error:
                    status, detail = "skipped", plugin.error
                else:
                    status, detail = "ok", "adds " + ", ".join(plugin.commands)
            elif group in registry.GROUP_KINDS:
                message = problems.get((group, ep.name))
                if message:
                    status, detail = "skipped", message
                elif registry.is_registered(registry.GROUP_KINDS[group], ep.name):
                    status = "ok"
            rows.append(
                InstalledPlugin(group, ep.name, ep.value, distribution, version, status, detail)
            )
    return rows


__all__ = [
    "CLI_ENTRY_POINT_GROUP",
    "PLUGIN_GROUPS",
    "InstalledPlugin",
    "LoadedCliPlugin",
    "command_names",
    "installed_plugins",
    "loaded_cli_plugins",
    "register_cli_plugins",
    "reset_cli_plugins",
]
