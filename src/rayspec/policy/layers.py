# SPDX-License-Identifier: Apache-2.0
"""Discovering ``policy.yaml`` files and combining them most-restrictive-wins.

Boundary: file discovery + merge + provenance. Three layers are read, highest precedence first:

1. ``$RAYSPEC_POLICY`` — a file named by the environment (the operator running the schedule);
2. ``<project>/.rayspec/policy.yaml`` — the checkout's own policy;
3. ``<home>/policy.yaml`` — the user's policy (``$RAYSPEC_HOME``, default ``~/.rayspec``).

"Precedence" only decides the order restrictions are *reported* in. It never decides which value
wins, because no layer can loosen another: allow-lists intersect, deny-lists unite, numeric caps
take the minimum and a boolean takes its restrictive side (an OR for a flag that forbids, an AND
for one that permits). A workflow has to satisfy every layer that is present.

Everything here is local. There is deliberately no key, flag or environment variable that fetches
a policy from a server, names an organisation or joins a shared registry: a rayspec process reads
files on the machine it runs on and nothing else.
"""

from __future__ import annotations

import errno
import fnmatch
import os
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rayspec.config.paths import rayspec_home
from rayspec.errors import LoaderError
from rayspec.policy.model import (
    ApprovalClassPolicy,
    ApprovalsPolicy,
    BudgetPolicy,
    Policy,
    access_rank,
)
from rayspec.schema import SchemaError

#: Environment variable naming an extra policy file, applied ahead of the project and user files.
POLICY_ENV = "RAYSPEC_POLICY"

#: File name of the project (``<project>/.rayspec/``) and user (``<home>/``) policy layers.
POLICY_FILENAME = "policy.yaml"

#: Layer names, highest precedence first.
LAYER_NAMES: tuple[str, ...] = (POLICY_ENV, "project", "user")


class PolicyError(LoaderError):
    """A ``policy.yaml`` cannot be read, parsed or validated.

    The message starts with ``<path>[:<line>]:`` so the CLI boundary prints it like any other
    load error and exits 2. A policy that cannot be read is never treated as an empty policy —
    silently dropping a guardrail is the one failure mode this module refuses.
    """


@dataclass(frozen=True, slots=True)
class PolicyPath:
    """One candidate layer: its name and the file it would be read from."""

    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class PolicySource:
    """The place a single restriction came from — layer, file, line and the offending value."""

    layer: str
    label: str
    line: int | None
    value: str

    @property
    def location(self) -> str:
        """``<file>:<line>`` (or just the file when the line is unknown)."""
        return self.label if self.line is None else f"{self.label}:{self.line}"

    def __str__(self) -> str:
        return f"{self.value!r} at {self.location}"


@dataclass(frozen=True, slots=True)
class PolicyLayer:
    """One loaded policy document plus the line map its provenance is built from."""

    name: str
    label: str
    path: Path
    policy: Policy
    lines: Mapping[tuple[str | int, ...], int] = field(default_factory=dict)

    def line_of(self, *keys: str | int) -> int | None:
        """1-based line of ``keys`` inside this document, or ``None`` when unknown."""
        return self.lines.get(tuple(keys))

    def source(self, value: object, *keys: str | int) -> PolicySource:
        """A :class:`PolicySource` for ``value`` located at ``keys`` in this layer."""
        return PolicySource(
            layer=self.name, label=self.label, line=self.line_of(*keys), value=str(value)
        )


def _held_classes(layer: PolicyLayer) -> tuple[PolicySource, ...]:
    """Every line of ONE layer that holds an approval class, with its own location.

    A class an operator merely named — ``allow_yes: true``, ``require_tty: false`` — takes
    nothing away and is not reported, for the same reason ``trust.require: false`` is not: a key
    that forbids nothing must not read as a restriction. The two rules are reported separately
    because they are separate lines a person would have to edit.
    """
    out: list[PolicySource] = []
    for name, entry in layer.policy.approvals.classes.items():
        if not entry.allow_yes:
            out.append(
                layer.source(f"{name}: allow_yes: false", "approvals", "classes", name, "allow_yes")
            )
        if entry.require_tty:
            out.append(
                layer.source(
                    f"{name}: require_tty: true", "approvals", "classes", name, "require_tty"
                )
            )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ChangeGuard:
    """The worktree change guard as the layers agreed on it.

    ``protected_paths`` is the union of every layer's globs (each with the layer that asked for
    it); the two caps are the smallest value any layer set, with every layer that set that value.
    """

    protected_paths: tuple[tuple[str, PolicySource], ...] = ()
    max_changed_files: tuple[int, tuple[PolicySource, ...]] | None = None
    max_changed_lines: tuple[int, tuple[PolicySource, ...]] | None = None

    @property
    def is_empty(self) -> bool:
        """True when no layer configured a guard at all."""
        return not (self.protected_paths or self.max_changed_files or self.max_changed_lines)


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """The layers that apply to one workflow, queried through restriction-shaped accessors.

    Consumers ask "is this forbidden, and by whom" rather than reading the raw documents: every
    accessor that can refuse returns the :class:`PolicySource` list that refuses (empty = allowed),
    so an error message can always name the layer and line a person has to edit.
    """

    layers: tuple[PolicyLayer, ...] = ()
    searched: tuple[str, ...] = ()

    # -- presence -----------------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        """True when no policy file was found at all (the default for a fresh project)."""
        return not self.layers

    @property
    def labels(self) -> tuple[str, ...]:
        """The display path of every layer in force, highest precedence first.

        The same spelling the error messages use, so "which layer denied this" and "which
        layers am I subject to" name the same files.
        """
        return tuple(layer.label for layer in self.layers)

    def layer(self, name: str) -> PolicyLayer | None:
        """The loaded layer called ``name`` (``project``, ``user``, ``RAYSPEC_POLICY``)."""
        return next((layer for layer in self.layers if layer.name == name), None)

    # -- providers ----------------------------------------------------------------------------

    def allowed_providers(self) -> frozenset[str] | None:
        """Intersection of every layer's ``providers.allow`` (``None`` = unrestricted)."""
        return self._intersect(lambda p: p.providers.allow)

    def provider_denied(self, provider: str) -> tuple[PolicySource, ...]:
        """The layers whose ``providers.allow`` leaves ``provider`` out (empty = allowed)."""
        out: list[PolicySource] = []
        for layer in self.layers:
            allow = layer.policy.providers.allow
            if allow is not None and provider not in allow:
                out.append(layer.source(", ".join(allow) or "(nothing)", "providers", "allow"))
        return tuple(out)

    # -- models -------------------------------------------------------------------------------

    def model_denied(self, model: str) -> tuple[PolicySource, ...]:
        """The layers whose ``models.deny`` matches ``model`` (exact id or glob)."""
        out: list[PolicySource] = []
        for layer in self.layers:
            for index, pattern in enumerate(layer.policy.models.deny):
                if model == pattern or fnmatch.fnmatchcase(model, pattern):
                    out.append(layer.source(pattern, "models", "deny", index))
        return tuple(out)

    # -- access -------------------------------------------------------------------------------

    def max_access(self) -> tuple[str, tuple[PolicySource, ...]] | None:
        """The least powerful ``access.max`` any layer set, with the layers that set it."""
        levels = [
            (layer, layer.policy.access.max)
            for layer in self.layers
            if layer.policy.access.max is not None
        ]
        if not levels:
            return None
        lowest = min(access_rank(level) for _, level in levels if level is not None)
        sources = tuple(
            layer.source(level, "access", "max")
            for layer, level in levels
            if level is not None and access_rank(level) == lowest
        )
        return sources[0].value, sources

    def access_exceeded(self, access: str) -> tuple[PolicySource, ...]:
        """The layers whose ``access.max`` is below ``access`` (empty = within policy)."""
        capped = self.max_access()
        if capped is None or access_rank(access) <= access_rank(capped[0]):
            return ()
        return capped[1]

    # -- tools --------------------------------------------------------------------------------

    def denied_tools(self) -> dict[str, tuple[PolicySource, ...]]:
        """Union of every layer's ``tools.deny``: entry → the layers that deny it."""
        out: dict[str, list[PolicySource]] = {}
        for layer in self.layers:
            for index, entry in enumerate(layer.policy.tools.deny):
                out.setdefault(entry, []).append(layer.source(entry, "tools", "deny", index))
        return {entry: tuple(sources) for entry, sources in out.items()}

    def tool_denied(self, entry: str) -> tuple[PolicySource, ...]:
        """The layers denying the tool entry ``entry`` (empty = allowed)."""
        return self.denied_tools().get(entry, ())

    # -- what any layer restricts at all ------------------------------------------------------

    def control_sources(self) -> dict[str, tuple[PolicySource, ...]]:
        """Policy key → the layers that restrict it, for EVERY key any layer has an opinion on.

        Used where the *presence* of a restriction matters rather than its value: it is what
        :func:`~rayspec.policy.enforce.check_provider_options` asks to learn whether an agent is
        governed at all, and therefore whether its ``provider_options`` block is read as an
        allow-list or waved through.

        Every key a layer can set appears here, including the ones no ``provider_options`` key
        could plausibly undo. That is deliberate: "is this run governed by a policy" has to be a
        question about the file rather than about a list of interesting keys, or the answer goes
        stale the day a key is added. Keys are spelled the way the file spells them
        (``tools.deny``, ``access.max``, ``models.deny``, ``mcp.allow_servers``,
        ``providers.allow``, ``trust.require``, ``approvals.classes``, ``workspace.*``,
        ``budget.*``, ``max_consecutive_failures``, ``max_concurrent_runs``).
        """
        out: dict[str, list[PolicySource]] = {}
        for layer in self.layers:
            policy = layer.policy
            for index, entry in enumerate(policy.tools.deny):
                out.setdefault("tools.deny", []).append(layer.source(entry, "tools", "deny", index))
            for index, pattern in enumerate(policy.models.deny):
                out.setdefault("models.deny", []).append(
                    layer.source(pattern, "models", "deny", index)
                )
            if policy.access.max is not None:
                out.setdefault("access.max", []).append(
                    layer.source(policy.access.max, "access", "max")
                )
            if policy.mcp.allow_servers is not None:
                out.setdefault("mcp.allow_servers", []).append(
                    layer.source(
                        ", ".join(policy.mcp.allow_servers) or "(nothing)", "mcp", "allow_servers"
                    )
                )
            if policy.providers.allow is not None:
                out.setdefault("providers.allow", []).append(
                    layer.source(
                        ", ".join(policy.providers.allow) or "(nothing)", "providers", "allow"
                    )
                )
            if policy.trust.require:
                out.setdefault("trust.require", []).append(layer.source("true", "trust", "require"))
            for source in _held_classes(layer):
                out.setdefault("approvals.classes", []).append(source)
            for index, pattern in enumerate(policy.workspace.protected_paths):
                out.setdefault("workspace.protected_paths", []).append(
                    layer.source(pattern, "workspace", "protected_paths", index)
                )
            for cap in ("max_changed_files", "max_changed_lines"):
                value = getattr(policy.workspace, cap)
                if value is not None:
                    out.setdefault(f"workspace.{cap}", []).append(
                        layer.source(value, "workspace", cap)
                    )
            for ceiling in ("per_run", "per_day", "per_month", "max_consecutive_failures"):
                value = getattr(policy.budget, ceiling)
                if value is not None:
                    out.setdefault(f"budget.{ceiling}", []).append(
                        layer.source(value, "budget", ceiling)
                    )
            for top in ("max_consecutive_failures", "max_concurrent_runs"):
                value = getattr(policy, top)
                if value is not None:
                    out.setdefault(top, []).append(layer.source(value, top))
        return {key: tuple(sources) for key, sources in out.items()}

    # -- mcp ----------------------------------------------------------------------------------

    def allowed_mcp_servers(self) -> frozenset[str] | None:
        """Intersection of every layer's ``mcp.allow_servers`` (``None`` = unrestricted)."""
        return self._intersect(lambda p: p.mcp.allow_servers)

    def mcp_denied(self, server: str) -> tuple[PolicySource, ...]:
        """The layers whose ``mcp.allow_servers`` leaves ``server`` out (empty = allowed)."""
        out: list[PolicySource] = []
        for layer in self.layers:
            allow = layer.policy.mcp.allow_servers
            if allow is not None and server not in allow:
                out.append(layer.source(", ".join(allow) or "(nothing)", "mcp", "allow_servers"))
        return tuple(out)

    # -- workspace ----------------------------------------------------------------------------

    def change_guard(self) -> ChangeGuard:
        """The merged worktree change guard (union of paths, minimum of the caps)."""
        protected: list[tuple[str, PolicySource]] = []
        for layer in self.layers:
            for index, pattern in enumerate(layer.policy.workspace.protected_paths):
                protected.append(
                    (pattern, layer.source(pattern, "workspace", "protected_paths", index))
                )
        return ChangeGuard(
            protected_paths=tuple(protected),
            max_changed_files=self._min_cap("max_changed_files"),
            max_changed_lines=self._min_cap("max_changed_lines"),
        )

    def workspace_sources(self) -> tuple[PolicySource, ...]:
        """Every layer line that configures the change guard (empty when no layer does)."""
        guard = self.change_guard()
        sources = [source for _, source in guard.protected_paths]
        for cap in (guard.max_changed_files, guard.max_changed_lines):
            if cap is not None:
                sources.extend(cap[1])
        return tuple(sources)

    # -- approvals ----------------------------------------------------------------------------

    @property
    def approvals(self) -> ApprovalsPolicy:
        """``approvals:`` as the layers agreed on it — most restrictive wins, names unite.

        The accessor carries the document key's own name for the same reason :attr:`budget` does:
        this is the object handed to
        :func:`~rayspec.engine.approval_classes.rules_from_policy`, and a consumer that reaches
        for ``.classes`` and silently gets nothing is a gate that is written down and not held.

        ``allow_yes: false`` wins over ``true`` and ``require_tty: true`` over ``false`` — a
        layer may only ever narrow what can approve a gate — and the set of class NAMES is the
        union, so a class only the user file defines is still defined.
        """
        merged: dict[str, ApprovalClassPolicy] = {}
        for layer in self.layers:
            for name, entry in layer.policy.approvals.classes.items():
                current = merged.get(name)
                merged[name] = ApprovalClassPolicy(
                    allow_yes=entry.allow_yes and (current is None or current.allow_yes),
                    require_tty=entry.require_tty or (current is not None and current.require_tty),
                )
        return ApprovalsPolicy(classes=merged)

    def approval_class_sources(self) -> tuple[PolicySource, ...]:
        """Every layer line that HOLDS an approval class (empty when no layer holds one)."""
        return tuple(source for layer in self.layers for source in _held_classes(layer))

    # -- the operational limits (read by rayspec.limits) ----------------------------------------

    @property
    def budget(self) -> BudgetPolicy:
        """``budget:`` as the layers agreed on it — the smallest ceiling any layer set.

        :mod:`rayspec.limits` reads this off the object :func:`load_policy` returns, so the
        accessor has to exist with the document key's own name: a consumer that reaches for
        ``policy.budget`` and silently gets ``None`` is a spending envelope that is written down
        and not applied.
        """
        return BudgetPolicy(
            per_run=self._min_of(lambda p: p.budget.per_run),
            per_day=self._min_of(lambda p: p.budget.per_day),
            per_month=self._min_of(lambda p: p.budget.per_month),
            max_consecutive_failures=self._min_int(lambda p: p.budget.max_consecutive_failures),
        )

    @property
    def max_consecutive_failures(self) -> int | None:
        """The top-level failure breaker: the smallest value any layer set."""
        return self._min_int(lambda p: p.max_consecutive_failures)

    @property
    def max_concurrent_runs(self) -> dict[str, int] | None:
        """``{provider: limit}`` (``"*"`` = every provider), smallest value any layer set.

        A bare integer is the same statement about every provider, so it is normalised to ``*``
        before the layers are combined — otherwise ``max_concurrent_runs: 1`` in one layer and
        ``{claude: 4}`` in another would read as two unrelated limits.
        """
        merged: dict[str, int] = {}
        found = False
        for layer in self.layers:
            value = layer.policy.max_concurrent_runs
            if value is None:
                continue
            found = True
            limits = {"*": value} if isinstance(value, int) else dict(value)
            for name, limit in limits.items():
                current = merged.get(name)
                merged[name] = limit if current is None else min(current, limit)
        return merged if found else None

    # -- trust --------------------------------------------------------------------------------

    def trust_required(self) -> tuple[PolicySource, ...]:
        """The layers demanding that a workflow be listed in ``.rayspec/trusted.yaml``."""
        return tuple(
            layer.source("true", "trust", "require")
            for layer in self.layers
            if layer.policy.trust.require
        )

    # -- internals ----------------------------------------------------------------------------

    def _intersect(self, pick: Any) -> frozenset[str] | None:
        allowed: frozenset[str] | None = None
        for layer in self.layers:
            values = pick(layer.policy)
            if values is None:
                continue
            current = frozenset(values)
            allowed = current if allowed is None else allowed & current
        return allowed

    def _min_of(self, pick: Any) -> float | None:
        """The smallest value any layer set for one numeric ceiling (``None`` = no layer set it)."""
        values = [pick(layer.policy) for layer in self.layers]
        present = [value for value in values if value is not None]
        return min(present) if present else None

    def _min_int(self, pick: Any) -> int | None:
        """:meth:`_min_of` for a whole-number ceiling."""
        value = self._min_of(pick)
        return None if value is None else int(value)

    def _min_cap(self, field_name: str) -> tuple[int, tuple[PolicySource, ...]] | None:
        values = [
            (layer, getattr(layer.policy.workspace, field_name))
            for layer in self.layers
            if getattr(layer.policy.workspace, field_name) is not None
        ]
        if not values:
            return None
        smallest = min(value for _, value in values)
        sources = tuple(
            layer.source(value, "workspace", field_name)
            for layer, value in values
            if value == smallest
        )
        return smallest, sources


def policy_paths(
    project_root: Path | None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[PolicyPath, ...]:
    """The three candidate policy files, highest precedence first (existing or not)."""
    env = os.environ if environ is None else environ
    home = rayspec_home(env) if home is None else home
    out: list[PolicyPath] = []
    named = env.get(POLICY_ENV)
    if named:
        out.append(PolicyPath(POLICY_ENV, Path(named).expanduser()))
    if project_root is not None:
        out.append(PolicyPath("project", Path(project_root) / ".rayspec" / POLICY_FILENAME))
    out.append(PolicyPath("user", Path(home) / POLICY_FILENAME))
    return tuple(out)


def short_path(path: Path, project_root: Path | None, home: Path) -> str:
    """Short display path: project-relative, ``~/.rayspec/…`` or the absolute path.

    Every location this package prints goes through it, policy layer or not: a refusal that
    names one file as ``.rayspec/policy.yaml`` and the next as a 90-character absolute path
    reads as two different kinds of thing.
    """
    if project_root is not None:
        try:
            return path.relative_to(project_root).as_posix()
        except ValueError:
            pass
    try:
        return "~/.rayspec/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _unusable(path: Path) -> str | None:
    """What is wrong with ``path`` as a policy file, or ``None`` when it is a regular file.

    ``"missing"`` is the one answer that means "no such layer"; everything else describes a path
    that exists in *some* shape and therefore must not be skipped. ``Path.is_file()`` cannot be
    used here: it answers ``False`` — swallowing the error — for a dangling symlink, a symlink
    loop, a directory and an unreadable parent alike, which is exactly how a guardrail disappears
    without anyone noticing.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return f"cannot be read: {exc.strerror or exc}"
    if stat.S_ISLNK(info.st_mode):
        try:
            info = os.stat(path)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return "is a symlink loop"
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                return "is a dangling symlink"
            return f"is a symlink that cannot be resolved: {exc.strerror or exc}"
    if stat.S_ISDIR(info.st_mode):
        return "is a directory"
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    return None


def _read_layer(candidate: PolicyPath, label: str) -> PolicyLayer:
    """Parse one policy document; every failure becomes a :class:`PolicyError`."""
    # lazy: rayspec.loader imports rayspec.policy (the validator's policy pass) — importing the
    # YAML reader at module load would close that cycle.
    from rayspec.loader.yaml import load_yaml_with_lines

    try:
        text = candidate.path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(f"{label}: policy file is not UTF-8: {exc}") from None
    except OSError as exc:
        raise PolicyError(f"{label}: cannot read policy file: {exc.strerror or exc}") from None
    try:
        data, lines = load_yaml_with_lines(text, source=label)
    except LoaderError as exc:
        raise PolicyError(str(exc), hint=exc.hint, location=exc.location) from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PolicyError(f"{label}: policy must be a mapping, got {type(data).__name__}")
    try:
        policy = Policy.parse(data, source=label)
    except SchemaError as exc:
        raise PolicyError(
            "; ".join(f"{label}: {e}" for e in exc.errors) or f"{label}: invalid policy",
            hint=exc.hint,
        ) from None
    return PolicyLayer(
        name=candidate.name, label=label, path=candidate.path, policy=policy, lines=lines
    )


def load_policy(
    project_root: Path | None,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> EffectivePolicy:
    """Read every policy layer that exists and return the combined restrictions.

    A missing file is simply an absent layer; a file that exists but cannot be read or parsed is
    a :class:`PolicyError`. ``$RAYSPEC_POLICY`` is the one exception: it was named explicitly, so
    a missing file there is an error rather than a silently skipped guardrail. The same file
    reached through two layers is loaded once, under the highest-precedence name.
    """
    env = os.environ if environ is None else environ
    home = rayspec_home(env) if home is None else home
    root = None if project_root is None else Path(project_root)
    layers: list[PolicyLayer] = []
    seen: set[Path] = set()
    candidates = policy_paths(root, home, env)
    # the searched list is deliberately NOT shortened against the project root: "which root was
    # this discovered against" is exactly the question it exists to answer.
    searched = tuple(short_path(c.path, None, Path(home)) for c in candidates)
    for candidate in candidates:
        label = short_path(candidate.path, root, Path(home))
        unusable = _unusable(candidate.path)
        if unusable == "missing":
            if candidate.name == POLICY_ENV:
                raise PolicyError(
                    f"{label}: no such policy file ({POLICY_ENV} names it)",
                    hint=f"unset {POLICY_ENV} or point it at a file that exists",
                )
            continue
        if unusable is not None:
            raise PolicyError(
                f"{label}: policy file {unusable}",
                hint="remove the path or make it a readable policy.yaml — a policy that cannot "
                "be read is never treated as an empty policy",
            )
        try:
            key = candidate.path.resolve()
        except OSError:  # pragma: no cover - resolve() on a live file
            key = candidate.path
        if key in seen:
            continue
        seen.add(key)
        layers.append(_read_layer(candidate, label))
    return EffectivePolicy(layers=tuple(layers), searched=searched)


def policy_note(layers: Sequence[str], searched: Sequence[str]) -> str:
    """One line naming the policy layers in force — the positive signal a control needs.

    "Silently absent" is the worst failure mode a guardrail has, so the empty case is the one
    that carries information: it names the paths that were looked at, unshortened, because
    policy is discovered against the project root and not against the workflow file.
    """
    if layers:
        return "policy: " + ", ".join(layers)
    where = ", ".join(searched)
    return f"policy: none in force (searched {where})" if where else "policy: none in force"


def sources_text(sources: Sequence[PolicySource] | Iterable[PolicySource]) -> str:
    """``.rayspec/policy.yaml:3, ~/.rayspec/policy.yaml:2`` — the locations of a restriction.

    Repeats are dropped: several entries of one deny-list share a line, and a message that names
    the same line three times reads as three problems.
    """
    return ", ".join(dict.fromkeys(source.location for source in sources))


__all__ = [
    "LAYER_NAMES",
    "POLICY_ENV",
    "POLICY_FILENAME",
    "ChangeGuard",
    "EffectivePolicy",
    "PolicyError",
    "PolicyLayer",
    "PolicyPath",
    "PolicySource",
    "load_policy",
    "policy_note",
    "policy_paths",
    "short_path",
    "sources_text",
]
