# SPDX-License-Identifier: Apache-2.0
"""PRD-07 R5: the cooperative cancel flag.

``rayspec cancel`` on a *live* run no longer signals the process — it writes a small marker
file (``cancel.json``) beside ``run.json``, in the run directory the store already owns. The
runner checks for it at step boundaries (:meth:`rayspec.engine.context.RunContext.check_cancelled`)
and, once it sees one, stops launching new work — a running step is left to finish, and
``join: always`` steps still run — then finalizes the run ``cancelled`` (exit 4). Not a
``kill``, and no new top-level storage location: one file, next to ``run.json``.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CANCEL_JSON = "cancel.json"


@dataclass(frozen=True, slots=True)
class CancelFlag:
    """The parsed content of a run's ``cancel.json``."""

    reason: str
    actor: str | None
    requested_at: datetime


def write_cancel_flag(run_dir: Path, *, reason: str, actor: str | None = None) -> None:
    """Write ``<run_dir>/cancel.json`` (tmp file + rename; ``0600``, matching the run store)."""
    from rayspec.store.file import open_private

    path = run_dir / CANCEL_JSON
    payload = {"reason": reason, "actor": actor, "requested_at": datetime.now(UTC).isoformat()}
    tmp = path.with_name(f"{CANCEL_JSON}.tmp")
    with open_private(tmp, "w") as fh:
        fh.write(json.dumps(payload, indent=2) + "\n")
    tmp.replace(path)


def read_cancel_flag(run_dir: Path) -> CancelFlag | None:
    """The parsed ``cancel.json`` of ``run_dir``, or ``None`` when there is none / unreadable."""
    path = run_dir / CANCEL_JSON
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("reason"), str):
        return None
    requested_at = datetime.now(UTC)
    stamp = raw.get("requested_at")
    if isinstance(stamp, str):
        with contextlib.suppress(ValueError):
            requested_at = datetime.fromisoformat(stamp)
    actor = raw.get("actor") if isinstance(raw.get("actor"), str) else None
    return CancelFlag(reason=raw["reason"], actor=actor, requested_at=requested_at)


__all__ = ["CANCEL_JSON", "CancelFlag", "read_cancel_flag", "write_cancel_flag"]
