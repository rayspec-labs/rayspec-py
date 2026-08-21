# SPDX-License-Identifier: Apache-2.0
"""StepPath — the canonical identity of a (possibly nested / iterated) step within a run.

Grammar::

    segment := id | id[n]
    path    := segment ('/' segment)*

Examples: ``assess``, ``build[2]/implement`` (loop iteration 2, 1-based), ``fix_all[0]/patch``
(each item 0, 0-based), ``review/lint`` (include), ``build[2]/fix_all[0]/patch`` (nested).
Paths are record keys (run.json, ``steps/<path>/`` on disk, events, CLI) — templates never see
them; template scoping is lexical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import total_ordering

from rayspec.schema.common import validate_identifier

_SEGMENT_RE = re.compile(r"^([a-z][a-z0-9_]*)(?:\[(\d+)\])?$")

Segment = tuple[str, int | None]


@total_ordering
@dataclass(frozen=True, slots=True)
class StepPath:
    segments: tuple[Segment, ...] = ()

    def _sort_key(self) -> tuple[tuple[str, int], ...]:
        return tuple((name, -1 if idx is None else idx) for name, idx in self.segments)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, StepPath):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    # -- construction -------------------------------------------------------------------------

    @classmethod
    def root(cls) -> StepPath:
        return cls(())

    @classmethod
    def parse(cls, text: str) -> StepPath:
        if text == "":
            return cls.root()
        segments: list[Segment] = []
        for raw in text.split("/"):
            match = _SEGMENT_RE.match(raw)
            if not match:
                raise ValueError(f"invalid step path {text!r}: bad segment {raw!r}")
            name, index = match.groups()
            validate_identifier(name)
            segments.append((name, int(index) if index is not None else None))
        return cls(tuple(segments))

    def child(self, step_id: str) -> StepPath:
        validate_identifier(step_id)
        return StepPath((*self.segments, (step_id, None)))

    def indexed(self, index: int) -> StepPath:
        """Attach an iteration/item index to the leaf segment (``build`` → ``build[2]``)."""
        if not self.segments:
            raise ValueError("cannot index the root path")
        if index < 0:
            raise ValueError("index must be non-negative")
        name, current = self.segments[-1]
        if current is not None:
            raise ValueError(f"segment {name!r} is already indexed ({current})")
        return StepPath((*self.segments[:-1], (name, index)))

    # -- navigation ---------------------------------------------------------------------------

    @property
    def parent(self) -> StepPath:
        return StepPath(self.segments[:-1])

    @property
    def leaf_id(self) -> str:
        if not self.segments:
            raise ValueError("root path has no leaf id")
        return self.segments[-1][0]

    @property
    def index(self) -> int | None:
        return self.segments[-1][1] if self.segments else None

    @property
    def depth(self) -> int:
        return len(self.segments)

    @property
    def is_root(self) -> bool:
        return not self.segments

    # -- rendering ----------------------------------------------------------------------------

    def __str__(self) -> str:
        return "/".join(
            f"{name}[{idx}]" if idx is not None else name for name, idx in self.segments
        )

    def fs_path(self) -> str:
        """Relative filesystem path under ``steps/``. Brackets are legal on POSIX and Windows."""
        return str(self)

    def matches(self, pattern: str) -> bool:
        """Glob match where ``*`` also spans indices: ``build[*]/implement``."""
        return fnmatchcase(_glob_safe(str(self)), _glob_safe(pattern))


def _glob_safe(text: str) -> str:
    # fnmatch treats [..] as a character class; our index brackets are literal.
    return text.replace("[", "<").replace("]", ">")


__all__ = ["Segment", "StepPath"]
