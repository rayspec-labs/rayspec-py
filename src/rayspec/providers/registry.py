# SPDX-License-Identifier: Apache-2.0
"""Provider registry: builtin registrations plus third-party discovery via entry points.

Boundary: this module never imports an SDK. Builtin registrations carry their capabilities
statically (:mod:`rayspec.providers.capabilities`) and a *lazy* factory that imports the adapter
module only when :func:`create_provider` is called, so validation, ``rayspec plan`` and
``rayspec providers`` work even when an adapter (or its SDK) is missing.

Third-party providers register through the entry-point group ``rayspec.providers``::

    [project.entry-points."rayspec.providers"]
    acme = "acme_rayspec:REGISTRATION"     # value = module:attribute

The attribute must be a :class:`~rayspec.providers.base.ProviderRegistration` whose ``id`` equals
the entry-point name. Precedence is fixed and order-independent: builtin ids (``claude``,
``codex``, ``stub``) can never be overridden; programmatic :func:`register` calls win over entry
points (an entry point with the same id is never loaded, or is displaced); entry points that
fail to load are skipped with a :class:`RuntimeWarning`.
"""

from __future__ import annotations

import difflib
import importlib
import warnings
from collections.abc import Mapping
from importlib.metadata import entry_points
from typing import Any

from rayspec.errors import RayspecError
from rayspec.providers.base import (
    Provider,
    ProviderCapabilities,
    ProviderError,
    ProviderFactory,
    ProviderNotInstalledError,
    ProviderRegistration,
)
from rayspec.providers.capabilities import (
    CLAUDE_CAPABILITIES,
    CODEX_CAPABILITIES,
    STUB_CAPABILITIES,
)

#: Entry-point group scanned for third-party providers (value = ``module:REGISTRATION``).
ENTRY_POINT_GROUP = "rayspec.providers"


class UnknownProviderError(RayspecError, LookupError):
    """``provider: <id>`` names no registered provider. ``hint`` carries did-you-mean."""


def _lazy_factory(provider_id: str, module_name: str, class_name: str) -> ProviderFactory:
    """Factory that imports ``module_name`` on first use and instantiates ``class_name``.

    ``ImportError`` → :class:`ProviderNotInstalledError` (kind ``not_installed``); any other
    exception raised while importing the adapter/SDK → :class:`ProviderError` (kind ``provider``)
    with a hint, chained to the original, so callers never see a raw traceback.
    """

    def factory(settings: Mapping[str, Any]) -> Provider:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ProviderNotInstalledError(
                "provider adapter not available",
                hint=(
                    f"the {provider_id!r} adapter ({module_name}) could not be imported: {exc}. "
                    f"Check the SDK install (`rayspec providers`); reinstall rayspec with the "
                    f"{provider_id} SDK."
                ),
            ) from exc
        except Exception as exc:
            raise ProviderError(
                "provider adapter failed to import",
                kind="provider",
                hint=(
                    f"importing the {provider_id!r} adapter ({module_name}) raised "
                    f"{type(exc).__name__}: {exc}. Check the SDK install (`rayspec providers`)."
                ),
            ) from exc
        cls = getattr(module, class_name, None)
        if cls is None:
            raise ProviderNotInstalledError(
                "provider adapter not available",
                hint=f"{module_name} defines no {class_name}; the rayspec install is broken",
            )
        return cls(settings)

    return factory


def _builtin(
    provider_id: str,
    display_name: str,
    capabilities: ProviderCapabilities,
    module_name: str,
    class_name: str,
) -> ProviderRegistration:
    return ProviderRegistration(
        id=provider_id,
        display_name=display_name,
        capabilities=capabilities,
        factory=_lazy_factory(provider_id, module_name, class_name),
    )


#: Builtin providers, in display order. Adapters are imported lazily by the factory.
BUILTIN_REGISTRATIONS: tuple[ProviderRegistration, ...] = (
    _builtin(
        "claude",
        "Claude Agent SDK",
        CLAUDE_CAPABILITIES,
        "rayspec.providers.claude",
        "ClaudeProvider",
    ),
    _builtin(
        "codex",
        "OpenAI Codex SDK",
        CODEX_CAPABILITIES,
        "rayspec.providers.codex",
        "CodexProvider",
    ),
    _builtin(
        "stub",
        "Stub (scripted)",
        STUB_CAPABILITIES,
        "rayspec.providers.stub",
        "StubProvider",
    ),
)
_BUILTIN_IDS: frozenset[str] = frozenset(r.id for r in BUILTIN_REGISTRATIONS)


class _State:
    """Module-level cache: resolved table + programmatic registrations."""

    table: dict[str, ProviderRegistration] | None = None
    programmatic: dict[str, ProviderRegistration] = {}


_state = _State()


def _discover_entry_points(known: Mapping[str, ProviderRegistration]) -> list[ProviderRegistration]:
    found: list[ProviderRegistration] = []
    try:
        eps = list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:  # pragma: no cover - metadata backends are exotic
        warnings.warn(
            f"rayspec: cannot scan entry points {ENTRY_POINT_GROUP!r}: {exc}", stacklevel=2
        )
        return found
    for ep in sorted(eps, key=lambda e: e.name):
        if ep.name in known or ep.name in _BUILTIN_IDS:
            continue  # builtins / earlier registrations win; never even load the module
        try:
            obj = ep.load()
        except Exception as exc:
            warnings.warn(
                f"rayspec: provider entry point {ep.name!r} ({ep.value}) failed to load: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        if not isinstance(obj, ProviderRegistration):
            warnings.warn(
                f"rayspec: provider entry point {ep.name!r} ({ep.value}) is not a "
                f"ProviderRegistration (got {type(obj).__name__}); skipped",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        if obj.id != ep.name:
            warnings.warn(
                f"rayspec: provider entry point {ep.name!r} registers id {obj.id!r}; "
                "the entry-point name must equal the provider id; skipped",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        found.append(obj)
    return found


def _load() -> dict[str, ProviderRegistration]:
    if _state.table is None:
        table: dict[str, ProviderRegistration] = {r.id: r for r in BUILTIN_REGISTRATIONS}
        table.update(_state.programmatic)
        for reg in _discover_entry_points(table):
            table[reg.id] = reg
        _state.table = table
    return _state.table


def reset_registry() -> None:
    """Forget the cached registry (and programmatic registrations). Intended for tests."""
    _state.table = None
    _state.programmatic.clear()


def register(registration: ProviderRegistration, *, replace: bool = False) -> None:
    """Register a provider programmatically (tests, embedding).

    Builtins can't be replaced. A programmatic registration always takes precedence over an
    entry point with the same id (whether discovered before or after this call); re-registering
    an id that was already registered programmatically needs ``replace=True``.
    """
    if registration.id in _BUILTIN_IDS:
        raise RayspecError(f"provider id {registration.id!r} is builtin and can not be replaced")
    if registration.id in _state.programmatic and not replace:
        raise RayspecError(
            f"provider {registration.id!r} is already registered (pass replace=True to override)"
        )
    _state.programmatic[registration.id] = registration
    _load()[registration.id] = registration


def list_registrations() -> list[ProviderRegistration]:
    """All known registrations: builtins first (claude, codex, stub), then others by id."""
    return list(_load().values())


def get_registration(provider_id: str) -> ProviderRegistration:
    """Look up a provider by id; raises :class:`UnknownProviderError` with did-you-mean."""
    table = _load()
    try:
        return table[provider_id]
    except KeyError:
        ids = sorted(table)
        close = difflib.get_close_matches(provider_id, ids, n=3, cutoff=0.6)
        if close:
            hint = "did you mean " + " or ".join(repr(c) for c in close) + "?"
        else:
            hint = f"available providers: {', '.join(ids)}"
        raise UnknownProviderError(f"unknown provider {provider_id!r}", hint=hint) from None


def create_provider(provider_id: str, settings: Mapping[str, Any] | None = None) -> Provider:
    """Instantiate a provider through its registration factory (lazy adapter import)."""
    registration = get_registration(provider_id)
    return registration.factory(dict(settings or {}))


__all__ = [
    "BUILTIN_REGISTRATIONS",
    "ENTRY_POINT_GROUP",
    "UnknownProviderError",
    "create_provider",
    "get_registration",
    "list_registrations",
    "register",
    "reset_registry",
]
