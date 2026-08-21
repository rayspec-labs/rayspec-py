# SPDX-License-Identifier: Apache-2.0
"""The model lockfile: ``.rayspec/rayspec.lock`` pins what each agent resolves to.

Module boundary: this module knows how the lockfile is spelled on disk, how it is derived from a
:class:`~rayspec.loader.ResolvedWorkflow` and how a resolved workflow is compared against it. It
runs nothing and opens no socket — the comparison is static, over the already-resolved agents.

Why it exists: ``model: sonnet`` (a tier), ``@fast`` (an alias) and an unset ``model:`` all mean
"whatever this resolves to today". Between the review of a change and its merge a provider can
change what that is, and nothing in the run record would have said so. The lockfile records the
literal model id and effort a workflow's agents resolved to; ``--locked`` refuses to run when
they resolve to something else, naming the agent, the pinned value and the resolved one.

The file is per project and is meant to be committed. It carries no secrets: agent keys,
provider ids, literal model ids and effort names only — never an input value, never an
environment variable.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rayspec.errors import RayspecError
from rayspec.loader import ResolvedWorkflow
from rayspec.loader.yaml import load_yaml

#: File name under the project's ``.rayspec/`` directory.
LOCKFILE_NAME = "rayspec.lock"

#: Format version written into the file; a newer major version is refused rather than guessed at.
LOCKFILE_VERSION = 1

_HEADER = (
    "# rayspec lockfile — the literal model id and effort every agent resolved to.\n"
    "# Regenerate with `rayspec lock`; check it with `rayspec lock --check`.\n"
    "# Runs enforce it with `--locked` (on by default under CI).\n"
)

#: ``CI`` values that do NOT mean "this is a CI run".
_FALSEY = frozenset({"", "0", "false", "no", "off"})


class LockfileError(RayspecError):
    """The lockfile exists but cannot be read as one."""


@dataclass(frozen=True, slots=True)
class LockEntry:
    """What one agent resolved to when the lockfile was written.

    ``model`` is ``None`` for an agent that resolves to no literal id — the provider's own
    default applies, which rayspec cannot pin. Such an agent is recorded (so the lockfile still
    lists it) but it can only ever drift by gaining a model, never by the provider changing its
    default underneath.
    """

    provider: str
    model: str | None = None
    effort: str | None = None

    def to_data(self) -> dict[str, Any]:
        """The mapping written to disk (keys always present, so a diff is readable)."""
        return {"provider": self.provider, "model": self.model, "effort": self.effort}


@dataclass(frozen=True, slots=True)
class LockDrift:
    """One difference between the lockfile and what the workflow resolves to now.

    ``field`` is ``workflow`` (the workflow has no lockfile entry at all), ``agent`` (the agent
    is not pinned), ``stale`` (the lockfile pins an agent the workflow no longer has) or the
    name of the field that differs (``provider``/``model``/``effort``).
    """

    agent: str
    field: str
    pinned: str | None
    resolved: str | None

    def message(self) -> str:
        """One line naming the agent, the pinned value and the resolved one."""
        if self.field == "workflow":
            return (
                f"workflow {self.agent!r} is not in the lockfile "
                f"(.rayspec/{LOCKFILE_NAME}) — run `rayspec lock`"
            )
        if self.field == "agent":
            return (
                f"agent {self.agent!r} is not pinned in the lockfile "
                f"(.rayspec/{LOCKFILE_NAME}) — run `rayspec lock`"
            )
        if self.field == "stale":
            return (
                f"the lockfile pins agent {self.agent!r}, which this workflow no longer has "
                f"(.rayspec/{LOCKFILE_NAME}) — run `rayspec lock`"
            )
        return (
            f"agent {self.agent!r} resolves to {self.field} {_show(self.resolved)} "
            f"but the lockfile pins {_show(self.pinned)}"
        )


def _show(value: str | None) -> str:
    """``'claude-sonnet-4-6'`` / ``the provider default`` — never a bare ``None``."""
    return repr(value) if value is not None else "the provider default"


@dataclass(frozen=True, slots=True)
class Lockfile:
    """A parsed lockfile: ``{workflow name: {agent key: LockEntry}}``."""

    version: int
    workflows: Mapping[str, Mapping[str, LockEntry]]
    path: Path | None = None

    def entries(self, workflow_name: str) -> Mapping[str, LockEntry] | None:
        """The pinned agents of ``workflow_name``, or ``None`` when it is not in the file."""
        return self.workflows.get(workflow_name)


def lockfile_path(project_root: Path) -> Path:
    """``<project root>/.rayspec/rayspec.lock`` (not created)."""
    return Path(project_root) / ".rayspec" / LOCKFILE_NAME


def lock_entries_for(resolved: ResolvedWorkflow) -> dict[str, LockEntry]:
    """What every agent a ``prompt:`` step resolves to should be pinned to.

    Keyed by the loader's opaque agent key (``agents.reviewer``, ``file:.rayspec/agents/x.yaml``,
    ``inline:<step path>`` …) — the same keys ``RunRecord.toolchain['models']`` uses, so a run
    record and the lockfile talk about the same agents. Only agents that a prompt step actually
    resolves to are pinned: an unused definition cannot change a run.
    """
    entries: dict[str, LockEntry] = {}
    for key in sorted(set(resolved.step_agents.values())):
        agent = resolved.agents.get(key)
        if agent is None:  # pragma: no cover - a step agent always resolves
            continue
        entries[key] = LockEntry(provider=agent.provider, model=agent.model, effort=agent.effort)
    return entries


def check_locked(resolved: ResolvedWorkflow, lockfile: Lockfile | None) -> list[LockDrift]:
    """Every difference between ``resolved`` and its lockfile entry (empty = in sync).

    ``lockfile=None`` (no file) is never drift: the lockfile is opt-in, and ``--locked`` reports
    the missing file itself. A workflow the file does not mention is one ``workflow`` drift; an
    agent it does not mention is one ``agent`` drift; an agent it pins that the workflow no
    longer has is one ``stale`` drift (otherwise ``lock --check`` would call a file up to date
    that ``lock`` then rewrites); anything else is one drift per field.
    """
    if lockfile is None:
        return []
    name = resolved.workflow.name
    pinned = lockfile.entries(name)
    if pinned is None:
        return [LockDrift(agent=name, field="workflow", pinned=None, resolved=None)]
    drifts: list[LockDrift] = []
    for key, entry in lock_entries_for(resolved).items():
        want = pinned.get(key)
        if want is None:
            drifts.append(LockDrift(agent=key, field="agent", pinned=None, resolved=entry.provider))
            continue
        for field_name in ("provider", "model", "effort"):
            have = getattr(entry, field_name)
            expected = getattr(want, field_name)
            if have != expected:
                drifts.append(
                    LockDrift(agent=key, field=field_name, pinned=expected, resolved=have)
                )
    resolved_keys = set(lock_entries_for(resolved))
    drifts += [
        LockDrift(agent=key, field="stale", pinned=pinned[key].model, resolved=None)
        for key in sorted(set(pinned) - resolved_keys)
    ]
    return drifts


def load_lockfile(project_root: Path) -> Lockfile | None:
    """Read ``.rayspec/rayspec.lock`` (``None`` when absent); raise :class:`LockfileError`.

    Strict YAML (the loader's reader: no duplicate keys, no surprise types) and a strict shape —
    a lockfile that cannot be trusted must not silently pass a ``--locked`` run.
    """
    path = lockfile_path(project_root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LockfileError(f"{path}: {exc}") from exc
    try:
        data = load_yaml(text, source=str(path))
    except RayspecError as exc:
        raise LockfileError(str(exc), hint=exc.hint) from exc
    return parse_lockfile(data, path=path)


def parse_lockfile(data: Any, *, path: Path | None = None) -> Lockfile:
    """Validate the parsed document (see :func:`load_lockfile`)."""
    where = str(path) if path is not None else LOCKFILE_NAME
    if not isinstance(data, Mapping):
        raise LockfileError(f"{where}: expected a mapping, got {type(data).__name__}")
    version = data.get("version", LOCKFILE_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise LockfileError(f"{where}: 'version' must be a positive integer")
    if version > LOCKFILE_VERSION:
        raise LockfileError(
            f"{where}: lockfile version {version} is newer than this rayspec understands "
            f"(version {LOCKFILE_VERSION})",
            hint="upgrade rayspec, or regenerate the lockfile with `rayspec lock`",
        )
    raw = data.get("workflows", {})
    if not isinstance(raw, Mapping):
        raise LockfileError(f"{where}: 'workflows' must be a mapping of workflow name to agents")
    workflows: dict[str, dict[str, LockEntry]] = {}
    for name, block in raw.items():
        workflows[str(name)] = _parse_agents(block, where=where, workflow=str(name))
    return Lockfile(version=version, workflows=workflows, path=path)


def _parse_agents(block: Any, *, where: str, workflow: str) -> dict[str, LockEntry]:
    if block is None:
        return {}
    if not isinstance(block, Mapping):
        raise LockfileError(f"{where}: workflows.{workflow} must be a mapping")
    agents = block.get("agents", {})
    if agents is None:
        return {}
    if not isinstance(agents, Mapping):
        raise LockfileError(f"{where}: workflows.{workflow}.agents must be a mapping")
    out: dict[str, LockEntry] = {}
    for key, entry in agents.items():
        if not isinstance(entry, Mapping):
            raise LockfileError(f"{where}: workflows.{workflow}.agents.{key} must be a mapping")
        provider = entry.get("provider")
        if not isinstance(provider, str) or not provider:
            raise LockfileError(
                f"{where}: workflows.{workflow}.agents.{key}.provider must be a provider id"
            )
        out[str(key)] = LockEntry(
            provider=provider,
            model=_opt_str(entry.get("model"), where, f"{workflow}.agents.{key}.model"),
            effort=_opt_str(entry.get("effort"), where, f"{workflow}.agents.{key}.effort"),
        )
    return out


def _opt_str(value: Any, where: str, field_path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LockfileError(f"{where}: workflows.{field_path} must be a string or null")
    return value


def write_lockfile(project_root: Path, workflows: Mapping[str, Mapping[str, LockEntry]]) -> Path:
    """Write ``.rayspec/rayspec.lock`` for ``workflows`` and return its path.

    Deterministic: workflow names and agent keys are sorted, so re-running ``rayspec lock``
    without a change produces byte-identical output and a clean diff. The file is replaced
    whole (temp file + ``os.replace``), so a crash or a full disk mid-write cannot leave a
    truncated lockfile that the next ``--locked`` run refuses to read.
    """
    path = lockfile_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "version": LOCKFILE_VERSION,
        "workflows": {
            name: {
                "agents": {key: workflows[name][key].to_data() for key in sorted(workflows[name])}
            }
            for name in sorted(workflows)
        },
    }
    text = _HEADER + yaml.safe_dump(body, sort_keys=False, default_flow_style=False)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return path


def merged_workflows(
    lockfile: Lockfile | None, updates: Mapping[str, Mapping[str, LockEntry]]
) -> dict[str, dict[str, LockEntry]]:
    """The existing pins with ``updates`` applied — re-locking one workflow keeps the others."""
    merged: dict[str, dict[str, LockEntry]] = {}
    if lockfile is not None:
        for name, entries in lockfile.workflows.items():
            merged[name] = dict(entries)
    for name, entries in updates.items():
        merged[name] = dict(entries)
    return merged


def locked_default(environ: Mapping[str, str]) -> bool:
    """Whether ``--locked`` is on when neither ``--locked`` nor ``--no-locked`` was given.

    On under ``CI`` — the whole point of the lockfile is that an unattended run does not quietly
    use a different model than the one a human reviewed. ``CI`` is what GitHub Actions, GitLab,
    Buildkite and CircleCI export; a runner that does not (Jenkins, TeamCity) needs ``--locked``
    spelled out. The default only enforces a lockfile that EXISTS — a project that never ran
    ``rayspec lock`` is not broken by setting a variable — while ``--locked`` refuses a missing
    one, because that is what asking for it means.
    """
    return environ.get("CI", "").strip().lower() not in _FALSEY


__all__ = [
    "LOCKFILE_NAME",
    "LOCKFILE_VERSION",
    "LockDrift",
    "LockEntry",
    "Lockfile",
    "LockfileError",
    "check_locked",
    "load_lockfile",
    "lock_entries_for",
    "locked_default",
    "lockfile_path",
    "merged_workflows",
    "parse_lockfile",
    "write_lockfile",
]
