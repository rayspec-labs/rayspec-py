# SPDX-License-Identifier: Apache-2.0
"""Registries for the pluggable runtime pieces: run stores, event sinks, approval prompts.

Module boundary: this module resolves an **id** to a **factory** and constructs it. It knows
nothing about runs — :class:`~rayspec.engine.runner.Runner` has always accepted ``store``,
``sinks`` and ``approval_prompt``; this is what makes those three discoverable, so a separate
package can drive the same engine without forking rayspec.

Three entry-point groups, all shaped like :mod:`rayspec.providers.registry`::

    [project.entry-points."rayspec.stores"]
    sqlite = "acme_rayspec:STORE"        # value = module:REGISTRATION

    [project.entry-points."rayspec.sinks"]
    notify = "acme_rayspec:SINK"

    [project.entry-points."rayspec.approvals"]
    policy = "acme_rayspec:APPROVAL"

Precedence is fixed and order-independent, exactly as for providers: builtin ids can never be
overridden, programmatic :func:`register_store` (…) calls win over entry points, and an entry
point that fails to load, is the wrong type, registers an id different from its name, or claims
an id another installed distribution already took is skipped with a :class:`RuntimeWarning` —
an id is first-come, so nothing is ever silently replaced. The builtins (``file`` store,
``console``/``json``/``quiet``/``null`` sinks, ``console`` approval) go through the same table,
so the code path a plugin takes is the one rayspec itself takes.

**Redaction boundary.** Secrets stop one layer ABOVE a third-party store:
:func:`create_store` wraps every store that did not come from the builtin table in
:class:`~rayspec.store.redacting.RedactingStore`, which redacts records, outputs, prompts,
events and stream records on their way in. A plugin store therefore never receives a secret and
cannot persist one, whatever it does with what it is handed. The builtin
:class:`~rayspec.store.file.FileRunStore` is the exception, and only because it applies the same
:class:`~rayspec.redact.Redactor` *inside* itself, closer to the bytes (it redacts a JSON output
on the parsed value, which the wrapper cannot do for a store it does not control). That
exemption is a property of the builtin table, not a flag a registration can set.
"""

from __future__ import annotations

import difflib
import warnings
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, TextIO, TypeVar

from rayspec.errors import RayspecError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rayspec.engine.approval import ApprovalPrompt
    from rayspec.events.base import EventSink
    from rayspec.store.base import RunStore

#: Extension kind → the entry-point group it is discovered from.
KIND_GROUPS: Mapping[str, str] = MappingProxyType(
    {"store": "rayspec.stores", "sink": "rayspec.sinks", "approval": "rayspec.approvals"}
)
#: The reverse mapping (``rayspec plugins`` groups its rows by entry-point group).
GROUP_KINDS: Mapping[str, str] = MappingProxyType({v: k for k, v in KIND_GROUPS.items()})

#: The shared empty settings mapping. It is handed out through a ``default_factory`` rather than
#: as a plain default: a dataclass field default must be hashable on Python 3.11, and a
#: ``mappingproxy`` is not (3.12 relaxed the check, which is how a package that cannot import on
#: its own declared floor passed a green suite). The factory returns this same instance, so
#: nothing is copied and it stays un-writable.
_EMPTY: Mapping[str, Any] = MappingProxyType({})


class UnknownExtensionError(RayspecError, LookupError):
    """An id names no registered store/sink/approval. ``hint`` carries did-you-mean."""


@dataclass(frozen=True)
class DiscoveryProblem:
    """One entry point that was refused, and why (``rayspec plugins`` prints these)."""

    kind: str
    group: str
    name: str
    value: str
    message: str


# ------------------------------------------------------------------------------------------
# what a factory is handed
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreContext:
    """What a store factory is given: where the runs of one project live, plus its settings."""

    root: Path
    home: Path
    project_slug: str = ""
    settings: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)


@dataclass(frozen=True)
class SinkContext:
    """What a sink factory is given: the streams the CLI writes to, plus its settings.

    ``console`` is a ``rich.console.Console`` (stderr) when one exists and ``stream`` the text
    stream a stdout-shaped sink should write to; both may be ``None`` when the caller has
    neither, and a factory that needs one is free to build its own.

    ``stream`` is ``None`` in particular whenever the CLI owns stdout itself — ``rayspec run
    --json`` puts the JSONL event stream there — and a sink handed no stream must not write to
    stdout anyway: it would interleave with the machine-readable output.
    """

    console: Any | None = None
    stream: TextIO | None = None
    verbose: bool = False
    quiet: bool = False
    settings: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)


@dataclass(frozen=True)
class ApprovalContext:
    """What an approval-prompt factory is given: whether the run may ask, plus its settings."""

    console: Any | None = None
    interactive: bool = True
    settings: Mapping[str, Any] = field(default_factory=lambda: _EMPTY)


# ------------------------------------------------------------------------------------------
# registrations
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreRegistration:
    """A run store under an id. ``factory(StoreContext)`` returns a ``RunStore``."""

    id: str
    display_name: str
    factory: Callable[[StoreContext], RunStore]


@dataclass(frozen=True)
class SinkRegistration:
    """An event sink under an id. ``factory(SinkContext)`` returns an ``EventSink``."""

    id: str
    display_name: str
    factory: Callable[[SinkContext], EventSink]


@dataclass(frozen=True)
class ApprovalRegistration:
    """An approval prompt under an id. ``factory(ApprovalContext)`` returns an
    ``ApprovalPrompt``."""

    id: str
    display_name: str
    factory: Callable[[ApprovalContext], ApprovalPrompt]


# ------------------------------------------------------------------------------------------
# builtins (lazy imports: resolving an id never loads rich or the store)
# ------------------------------------------------------------------------------------------


def _file_store(context: StoreContext) -> RunStore:
    from rayspec.store.file import FileRunStore

    return FileRunStore(context.root)


def _console_sink(context: SinkContext) -> EventSink:
    from rayspec.events.sinks import ConsoleSink

    return ConsoleSink(_console(context.console), verbose=context.verbose, quiet=context.quiet)


def _quiet_sink(context: SinkContext) -> EventSink:
    from rayspec.events.sinks import QuietConsoleSink

    return QuietConsoleSink(_console(context.console), show_started=context.verbose)


def _json_sink(context: SinkContext) -> EventSink:
    import sys

    from rayspec.events.sinks import JsonStdoutSink

    return JsonStdoutSink(context.stream if context.stream is not None else sys.stdout)


def _null_sink(context: SinkContext) -> EventSink:
    from rayspec.events.sinks import NullSink

    return NullSink()


def _console_approval(context: ApprovalContext) -> ApprovalPrompt:
    from rayspec.engine.approval import ConsoleApprovalPrompt

    return ConsoleApprovalPrompt(context.console)


def _console(console: Any | None) -> Any:
    """The given rich console, or a fresh stderr one (the CLI's default)."""
    if console is not None:
        return console
    from rich.console import Console

    return Console(stderr=True)


#: Builtin stores, in display order.
BUILTIN_STORES: tuple[StoreRegistration, ...] = (
    StoreRegistration("file", "File run store ($RAYSPEC_HOME)", _file_store),
)
#: Builtin sinks, in display order.
BUILTIN_SINKS: tuple[SinkRegistration, ...] = (
    SinkRegistration("console", "Console step tree", _console_sink),
    SinkRegistration("json", "JSON lines on stdout", _json_sink),
    SinkRegistration("quiet", "One line per event", _quiet_sink),
    SinkRegistration("null", "Discards everything", _null_sink),
)
#: Builtin approval prompts, in display order.
BUILTIN_APPROVALS: tuple[ApprovalRegistration, ...] = (
    ApprovalRegistration("console", "Interactive terminal prompt", _console_approval),
)


# ------------------------------------------------------------------------------------------
# the shared registry machinery
# ------------------------------------------------------------------------------------------

R = TypeVar("R", StoreRegistration, SinkRegistration, ApprovalRegistration)


class _Registry(Generic[R]):
    """One kind's table: builtins, programmatic registrations, then entry points."""

    def __init__(self, kind: str, registration_type: type[R], builtins: Iterable[R]) -> None:
        self.kind = kind
        self.group = KIND_GROUPS[kind]
        self.registration_type = registration_type
        self.builtins: tuple[R, ...] = tuple(builtins)
        self.builtin_ids = frozenset(r.id for r in self.builtins)
        self.programmatic: dict[str, R] = {}
        self.problems: list[DiscoveryProblem] = []
        self.table: dict[str, R] | None = None

    def reset(self) -> None:
        self.table = None
        self.programmatic.clear()
        self.problems.clear()

    def load(self) -> dict[str, R]:
        if self.table is None:
            table: dict[str, R] = {r.id: r for r in self.builtins}
            table.update(self.programmatic)
            self.problems.clear()
            for registration in self._discover(table):
                table[registration.id] = registration
            self.table = table
        return self.table

    def _refuse(self, ep: EntryPoint, message: str) -> None:
        self.problems.append(DiscoveryProblem(self.kind, self.group, ep.name, ep.value, message))
        warnings.warn(
            f"rayspec: {self.kind} entry point {ep.name!r} ({ep.value}) {message}; skipped",
            RuntimeWarning,
            stacklevel=2,
        )

    def _discover(self, known: Mapping[str, R]) -> list[R]:
        try:
            eps = sorted(entry_points(group=self.group), key=lambda ep: ep.name)
        except Exception as exc:  # pragma: no cover - metadata backends are exotic
            warnings.warn(
                f"rayspec: cannot scan entry points {self.group!r}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            return []
        found: list[R] = []
        claimed = set(known)  # grows as ids are accepted: the FIRST distribution keeps an id
        for ep in eps:
            if ep.name in self.builtin_ids:
                # never even load the module: a builtin id is not up for grabs
                self._refuse(ep, f"uses the builtin {self.kind} id {ep.name!r}")
                continue
            if ep.name in known:
                continue  # a programmatic registration wins, whenever it was made
            if ep.name in claimed:
                # two installed distributions publishing one id: whichever the metadata
                # backend enumerated first keeps it, and the other is visible in `rayspec
                # plugins` instead of quietly overwriting it
                self._refuse(ep, f"id {ep.name!r} is already provided by another distribution")
                continue
            try:
                obj = ep.load()
            except Exception as exc:
                self._refuse(ep, f"failed to load: {type(exc).__name__}: {exc}")
                continue
            if not isinstance(obj, self.registration_type):
                self._refuse(
                    ep,
                    f"is not a {self.registration_type.__name__} (got {type(obj).__name__})",
                )
                continue
            if obj.id != ep.name:
                self._refuse(
                    ep,
                    f"registers id {obj.id!r}; the entry-point name must equal the id",
                )
                continue
            claimed.add(obj.id)
            found.append(obj)
        return found

    def register(self, registration: R, *, replace: bool = False) -> None:
        if registration.id in self.builtin_ids:
            raise RayspecError(
                f"{self.kind} id {registration.id!r} is builtin and can not be replaced"
            )
        if registration.id in self.programmatic and not replace:
            raise RayspecError(
                f"{self.kind} {registration.id!r} is already registered "
                "(pass replace=True to override)"
            )
        self.programmatic[registration.id] = registration
        self.load()[registration.id] = registration

    def get(self, extension_id: str) -> R:
        table = self.load()
        try:
            return table[extension_id]
        except KeyError:
            ids = sorted(table)
            close = difflib.get_close_matches(extension_id, ids, n=3, cutoff=0.6)
            hint = (
                "did you mean " + " or ".join(repr(c) for c in close) + "?"
                if close
                else f"available {self.kind}s: {', '.join(ids)}"
            )
            raise UnknownExtensionError(
                f"unknown {self.kind} {extension_id!r}", hint=hint
            ) from None

    def list(self) -> list[R]:
        return list(self.load().values())


@dataclass
class _State:
    """The three tables (module-level caches, reset together)."""

    store: _Registry[StoreRegistration] = field(
        default_factory=lambda: _Registry("store", StoreRegistration, BUILTIN_STORES)
    )
    sink: _Registry[SinkRegistration] = field(
        default_factory=lambda: _Registry("sink", SinkRegistration, BUILTIN_SINKS)
    )
    approval: _Registry[ApprovalRegistration] = field(
        default_factory=lambda: _Registry("approval", ApprovalRegistration, BUILTIN_APPROVALS)
    )

    def all(self) -> tuple[_Registry[Any], ...]:
        return (self.store, self.sink, self.approval)


_state = _State()


def reset_registry() -> None:
    """Forget every cached table (and programmatic registrations). Intended for tests."""
    for registry in _state.all():
        registry.reset()


def discovery_problems() -> tuple[DiscoveryProblem, ...]:
    """Every entry point refused during the last discovery, across the three groups."""
    return tuple(problem for registry in _state.all() for problem in registry.problems)


def is_registered(kind: str, extension_id: str) -> bool:
    """True when ``extension_id`` resolves for ``kind`` (``store``/``sink``/``approval``)."""
    if kind not in KIND_GROUPS:
        return False
    registry: _Registry[Any] = getattr(_state, kind)
    return extension_id in registry.load()


# ------------------------------------------------------------------------------------------
# public per-kind surface
# ------------------------------------------------------------------------------------------


def get_store(store_id: str) -> StoreRegistration:
    """Look up a run store by id; raises :class:`UnknownExtensionError` with did-you-mean."""
    return _state.store.get(store_id)


def list_stores() -> list[StoreRegistration]:
    """Every registered run store: builtins first, then plugins by id."""
    return _state.store.list()


def register_store(registration: StoreRegistration, *, replace: bool = False) -> None:
    """Register a run store programmatically (tests, embedding). Builtins are immutable."""
    _state.store.register(registration, replace=replace)


def create_store(store_id: str, context: StoreContext) -> RunStore:
    """Build the store registered as ``store_id``, redaction-safe by construction.

    A store that is not builtin is returned wrapped in
    :class:`~rayspec.store.redacting.RedactingStore`: the run's secrets are replaced *before*
    the plugin sees them. Assign the run's redactor to the returned store's ``redactor``
    attribute the way the CLI does for the builtin one.
    """
    registration = get_store(store_id)
    store = registration.factory(context)
    if store_id in _state.store.builtin_ids:
        return store
    from rayspec.store.redacting import RedactingStore

    return RedactingStore(store)


def get_sink(sink_id: str) -> SinkRegistration:
    """Look up an event sink by id; raises :class:`UnknownExtensionError` with did-you-mean."""
    return _state.sink.get(sink_id)


def list_sinks() -> list[SinkRegistration]:
    """Every registered event sink: builtins first, then plugins by id."""
    return _state.sink.list()


def register_sink(registration: SinkRegistration, *, replace: bool = False) -> None:
    """Register an event sink programmatically (tests, embedding). Builtins are immutable."""
    _state.sink.register(registration, replace=replace)


def create_sink(sink_id: str, context: SinkContext) -> EventSink:
    """Build the sink registered as ``sink_id``.

    Redaction is not this seam's business: the CLI wraps EVERY sink of a run in
    :class:`~rayspec.redact.RedactingSink` where it assembles them, which covers a sink that
    arrived through an entry point exactly like a builtin one.
    """
    return get_sink(sink_id).factory(context)


def get_approval(approval_id: str) -> ApprovalRegistration:
    """Look up an approval prompt by id; raises :class:`UnknownExtensionError`."""
    return _state.approval.get(approval_id)


def list_approvals() -> list[ApprovalRegistration]:
    """Every registered approval prompt: builtins first, then plugins by id."""
    return _state.approval.list()


def register_approval(registration: ApprovalRegistration, *, replace: bool = False) -> None:
    """Register an approval prompt programmatically. Builtins are immutable."""
    _state.approval.register(registration, replace=replace)


def create_approval(approval_id: str, context: ApprovalContext) -> ApprovalPrompt:
    """Build the approval prompt registered as ``approval_id``."""
    return get_approval(approval_id).factory(context)


__all__ = [
    "BUILTIN_APPROVALS",
    "BUILTIN_SINKS",
    "BUILTIN_STORES",
    "GROUP_KINDS",
    "KIND_GROUPS",
    "ApprovalContext",
    "ApprovalRegistration",
    "DiscoveryProblem",
    "SinkContext",
    "SinkRegistration",
    "StoreContext",
    "StoreRegistration",
    "UnknownExtensionError",
    "create_approval",
    "create_sink",
    "create_store",
    "discovery_problems",
    "get_approval",
    "get_sink",
    "get_store",
    "is_registered",
    "list_approvals",
    "list_sinks",
    "list_stores",
    "register_approval",
    "register_sink",
    "register_store",
    "reset_registry",
]
