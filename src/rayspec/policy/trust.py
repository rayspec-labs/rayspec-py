# SPDX-License-Identifier: Apache-2.0
"""``.rayspec/trusted.yaml`` — the workflows this checkout is allowed to run.

Boundary: one local file, read and written here and nowhere else. An entry is a workflow's label
plus the hash the loader computed for it, so trust is a statement about *content*, not about a
name someone can point at a different file later.

What the hash covers decides whether the gate is real. ``ResolvedWorkflow.hash`` is taken over
every file that contributed to the resolved workflow — the document itself, every ``include:``d
body, every agent file, every ``prompt_file``/``instructions_file`` — so editing an included body
or an agent's instructions revokes trust exactly the way editing the workflow does. A gate that
only hashed the entry document would be theatre.

This file belongs in the repository next to ``policy.yaml``: it is reviewed like code, and it
carries nothing secret — a path and a digest.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from rayspec.errors import LoaderError
from rayspec.policy.layers import PolicyError

if TYPE_CHECKING:  # type-only: importing the loader at runtime would close an import cycle
    from rayspec.loader.loader import ResolvedWorkflow

#: File name of the per-project trust list, next to ``policy.yaml`` in ``.rayspec/``.
TRUSTED_FILENAME = "trusted.yaml"

#: Prefix of a recorded digest — the algorithm is written down so it can change one day.
HASH_PREFIX = "sha256:"


def trusted_path(project_root: Path) -> Path:
    """``<project_root>/.rayspec/trusted.yaml``."""
    return Path(project_root) / ".rayspec" / TRUSTED_FILENAME


def qualified(digest: str) -> str:
    """``sha256:<hex>`` for a bare digest; unchanged when it already carries an algorithm."""
    return digest if ":" in digest else HASH_PREFIX + digest


@dataclass(frozen=True, slots=True)
class TrustEntry:
    """One trusted workflow: its label, the hash that was trusted and when it was added."""

    workflow: str
    hash: str
    added: str = ""

    def to_data(self) -> dict[str, str]:
        data = {"workflow": self.workflow, "hash": self.hash}
        if self.added:
            data["added"] = self.added
        return data


@dataclass(frozen=True, slots=True)
class TrustStore:
    """The parsed ``trusted.yaml`` of one project (immutable; ``add``/``remove`` return a copy)."""

    path: Path
    entries: tuple[TrustEntry, ...] = ()

    # -- loading ------------------------------------------------------------------------------

    @classmethod
    def load(cls, project_root: Path) -> TrustStore:
        """Read the project's trust list; a missing file is an empty store.

        A file that exists but cannot be read or parsed is a :class:`PolicyError` — never an
        empty store, because "the allow-list was unreadable" must not read as "nothing is
        allowed to be checked".
        """
        path = trusted_path(project_root)
        if not path.is_file():
            return cls(path=path)
        # lazy: rayspec.loader imports rayspec.policy (the validator's policy pass)
        from rayspec.loader.yaml import load_yaml

        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise PolicyError(f"{path}: cannot read trust list: {exc}") from None
        try:
            data = load_yaml(text, source=str(path))
        except LoaderError as exc:
            raise PolicyError(str(exc), hint=exc.hint, location=exc.location) from None
        return cls(path=path, entries=_parse(data, path))

    # -- queries ------------------------------------------------------------------------------

    def entry_for(self, label: str) -> TrustEntry | None:
        """The entry listed for the workflow ``label``, if any."""
        return next((e for e in self.entries if e.workflow == label), None)

    def is_trusted(self, resolved: ResolvedWorkflow) -> bool:
        """Whether ``resolved`` is listed *and* still hashes to what was listed."""
        entry = self.entry_for(resolved.label)
        return entry is not None and entry.hash == qualified(resolved.hash)

    def problem_for(self, resolved: ResolvedWorkflow) -> str | None:
        """Why ``resolved`` is not trusted (``None`` when it is) — phrased for an error message."""
        entry = self.entry_for(resolved.label)
        if entry is None:
            return f"is not in {self._label()}"
        if entry.hash != qualified(resolved.hash):
            return f"hash has changed since it was added to {self._label()}"
        return None

    def _label(self) -> str:
        return f".rayspec/{TRUSTED_FILENAME}"

    # -- editing ------------------------------------------------------------------------------

    def add(self, resolved: ResolvedWorkflow, *, now: str | None = None) -> tuple[TrustStore, bool]:
        """Trust ``resolved`` at its current hash; returns the new store and whether it replaced."""
        entry = TrustEntry(
            workflow=resolved.label,
            hash=qualified(resolved.hash),
            added=now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        kept = [e for e in self.entries if e.workflow != entry.workflow]
        replaced = len(kept) != len(self.entries)
        entries = tuple(sorted([*kept, entry], key=lambda e: e.workflow))
        return replace(self, entries=entries), replaced

    def remove(self, label: str) -> tuple[TrustStore, bool]:
        """Drop the entry for ``label``; returns the new store and whether anything was removed."""
        kept = tuple(e for e in self.entries if e.workflow != label)
        return replace(self, entries=kept), len(kept) != len(self.entries)

    def save(self) -> None:
        """Write the list atomically (an empty list removes the file)."""
        if not self.entries:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            return
        data = {"workflows": [e.to_data() for e in self.entries]}
        text = yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(tmp, 0o644 & ~_umask())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise


def _umask() -> int:
    """The process umask (read by setting and restoring it — there is no getter)."""
    current = os.umask(0o022)
    os.umask(current)
    return current


def _parse(data: Any, path: Path) -> tuple[TrustEntry, ...]:
    if data is None:
        return ()
    if not isinstance(data, dict):
        raise PolicyError(f"{path}: trust list must be a mapping, got {type(data).__name__}")
    rows = data.get("workflows", [])
    unknown = sorted(k for k in data if k != "workflows")
    if unknown:
        raise PolicyError(f"{path}: unknown field {unknown[0]!r} for trust list")
    if not isinstance(rows, list):
        raise PolicyError(f"{path}: 'workflows' must be a list, got {type(rows).__name__}")
    out: list[TrustEntry] = []
    for index, row in enumerate(rows):
        where = f"{path}: workflows[{index}]"
        if not isinstance(row, dict):
            raise PolicyError(f"{where} must be a mapping, got {type(row).__name__}")
        workflow, digest = row.get("workflow"), row.get("hash")
        if not isinstance(workflow, str) or not workflow:
            raise PolicyError(f"{where} needs a 'workflow' path")
        if not isinstance(digest, str) or not digest:
            raise PolicyError(f"{where} needs a 'hash'")
        added = row.get("added")
        out.append(
            TrustEntry(
                workflow=workflow,
                hash=qualified(digest),
                added=added if isinstance(added, str) else "",
            )
        )
    return tuple(out)


__all__ = [
    "HASH_PREFIX",
    "TRUSTED_FILENAME",
    "TrustEntry",
    "TrustStore",
    "qualified",
    "trusted_path",
]
