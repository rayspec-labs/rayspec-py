# SPDX-License-Identifier: Apache-2.0
"""Shared schema primitives: identifiers, durations, status enums."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator

#: Identifiers for step ids, input names, agent names, output keys and ``as:`` variables.
IDENT_RE = re.compile(r"[a-z][a-z0-9_]*")

#: Names that are roots of the template context (or Jinja specials) and therefore can't be ids.
RESERVED_ROOTS: frozenset[str] = frozenset(
    {
        "inputs",
        "steps",
        "run",
        "project",
        "env",
        "iteration",
        "each",
        "loop",
        "self",
        "true",
        "false",
        "none",
        "null",
    }
)

JoinPolicy = Literal["all", "any", "always"]
Isolation = Literal["worktree", "none"]
OnUnsupported = Literal["error", "warn"]
OnStepFailure = Literal["drain", "fail_fast", "continue"]
AccessLevelName = Literal["read-only", "workspace-write", "full"]
InstructionsModeName = Literal["append", "replace"]
EffortName = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def validate_identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENT_RE.fullmatch(value):
        raise ValueError(
            f"invalid identifier {value!r}: must match ^[a-z][a-z0-9_]*$ (lowercase snake_case)"
        )
    if value in RESERVED_ROOTS:
        raise ValueError(f"invalid identifier {value!r}: reserved name (template context root)")
    return value


Identifier = Annotated[str, AfterValidator(validate_identifier)]


def validate_name(value: str) -> str:
    """Identifier syntax without the reserved-root check (dict keys: inputs/agents/outputs)."""
    if not isinstance(value, str) or not IDENT_RE.fullmatch(value):
        raise ValueError(
            f"invalid name {value!r}: must match ^[a-z][a-z0-9_]*$ (lowercase snake_case)"
        )
    return value


Name = Annotated[str, AfterValidator(validate_name)]

_DURATION_RE = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m(?!s))?\s*(?:(\d+(?:\.\d+)?)s)?\s*(?:(\d+)ms)?\s*$"
)
_DURATION_HINT = "use seconds (e.g. 90) or a string like '90s', '10m', '1h30m', '500ms'"


def parse_duration(value: object) -> float:
    """Parse a duration into seconds. Accepts non-negative numbers or ``h/m/s/ms`` strings."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid duration {value!r}; {_DURATION_HINT}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError(f"invalid duration {value!r}: must not be negative")
        return float(value)
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match and any(match.groups()):
            hours, minutes, seconds, millis = match.groups()
            return (
                float(hours or 0) * 3600
                + float(minutes or 0) * 60
                + float(seconds or 0)
                + float(millis or 0) / 1000
            )
    raise ValueError(f"invalid duration {value!r}; {_DURATION_HINT}")


Duration = Annotated[float, BeforeValidator(parse_duration)]


def _positive(value: float) -> float:
    if value <= 0:
        raise ValueError("must be greater than 0")
    return value


PositiveDuration = Annotated[float, BeforeValidator(parse_duration), AfterValidator(_positive)]


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STEP_STATUSES


_TERMINAL_STEP_STATUSES = frozenset(
    {
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.INTERRUPTED,
        StepStatus.REJECTED,
    }
)


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


__all__ = [
    "IDENT_RE",
    "RESERVED_ROOTS",
    "AccessLevelName",
    "Duration",
    "EffortName",
    "Identifier",
    "InstructionsModeName",
    "Isolation",
    "JoinPolicy",
    "Name",
    "OnStepFailure",
    "OnUnsupported",
    "PositiveDuration",
    "RunStatus",
    "StepStatus",
    "parse_duration",
    "validate_identifier",
    "validate_name",
]
