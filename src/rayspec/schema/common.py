# SPDX-License-Identifier: Apache-2.0
"""Shared schema primitives: identifiers, durations, status enums."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator

#: Identifiers for step ids, input names, agent names, output keys and ``as:`` variables.
IDENT_RE = re.compile(r"[a-z][a-z0-9_]*")

#: Longest identifier rayspec accepts, in characters.
#:
#: Every identifier ends up as a **path segment**: a workflow is
#: ``.rayspec/workflows/<name>.yaml``, its worktree is ``<name>-<short run id>`` with a git ref
#: file ``.git/refs/heads/rayspec/<name>-<short run id>.lock`` beside it, a step is
#: ``steps/<id>/`` inside the run directory. ``NAME_MAX`` is 255 bytes on macOS and Linux alike,
#: so the longest identifier a filesystem can carry depends on which suffix a given command
#: happens to append — and without a bound of rayspec's own the refusal is whatever the
#: filesystem said (``[Errno 63] File name too long``, a bare errno naming no rule). 128 is half
#: of ``NAME_MAX``: it clears every suffix rayspec appends with room to spare, and it is far more
#: than a readable name needs. It is a rule of the identifier, so it lives with the other ones
#: and every identifier — in a workflow file, in ``rayspec new``, in an ``agents:`` key — is
#: refused by the same message.
MAX_IDENT_LEN = 128

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


def _too_long(kind: str, value: str) -> str:
    """The over-length message: names the limit and the length, and never echoes the whole value.

    A 300-character identifier printed back in full buries the rule it broke under 300
    characters of the mistake.
    """
    return (
        f"invalid {kind} {_ellipsis(value)}: must be at most {MAX_IDENT_LEN} characters "
        f"(this one is {len(value)}) — it becomes a file name"
    )


def _ellipsis(value: str, keep: int = 32) -> str:
    """``value`` as a repr, shortened to ``keep`` characters; the length is named by the caller."""
    return repr(value) if len(value) <= keep else f"{value[:keep]!r}…"


def validate_identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENT_RE.fullmatch(value):
        raise ValueError(
            f"invalid identifier {value!r}: must match ^[a-z][a-z0-9_]*$ (lowercase snake_case)"
        )
    if len(value) > MAX_IDENT_LEN:
        raise ValueError(_too_long("identifier", value))
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
    if len(value) > MAX_IDENT_LEN:
        raise ValueError(_too_long("name", value))
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


_MONEY_RE = re.compile(r"^\s*\$?\s*(\d+(?:\.\d+)?)\s*(?:USD)?\s*$", re.IGNORECASE)
_MONEY_HINT = "use a USD amount (e.g. 1.5) or a string like '$1.50'"


def parse_money(value: object) -> float:
    """Parse a USD cap into a float (``1.5``, ``"1.50"``, ``"$1.50"``, ``"12 USD"``).

    Lives here (not in ``schema.workflow``) so ``schema.agent`` — which ``schema.workflow``
    imports — can reach it without a cycle; ``schema.workflow`` re-exports ``Money``/``parse_money``
    for the callers CONTRACTS names.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid amount {value!r}; {_MONEY_HINT}")
    if isinstance(value, int | float):
        amount = float(value)
    elif isinstance(value, str):
        match = _MONEY_RE.match(value)
        if match is None:
            raise ValueError(f"invalid amount {value!r}; {_MONEY_HINT}")
        amount = float(match.group(1))
    else:
        raise ValueError(f"invalid amount {value!r}; {_MONEY_HINT}")
    if amount <= 0:
        raise ValueError("must be greater than 0")
    return amount


#: ``defaults.budget_usd``: positive USD amount; accepts ``"$1.50"`` strings.
Money = Annotated[float, BeforeValidator(parse_money)]

#: An agent number backed by an input: EXACTLY ``{{ inputs.<name> }}`` (whitespace tolerant),
#: nothing else — not an arithmetic expression, not a partial template. This is the one place a
#: numeric field accepts a reference, and it is a reference, not an expression (constitution: a
#: lever the workflow author sets, resolved to the run's own input, not computed).
INPUT_REF_RE = re.compile(r"^\s*\{\{\s*inputs\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}\s*$")


def input_ref_name(value: object) -> str | None:
    """The input name in ``{{ inputs.<name> }}``, or ``None`` when ``value`` is not exactly one."""
    if not isinstance(value, str):
        return None
    match = INPUT_REF_RE.match(value)
    return match.group(1) if match else None


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
    "INPUT_REF_RE",
    "MAX_IDENT_LEN",
    "RESERVED_ROOTS",
    "AccessLevelName",
    "Duration",
    "EffortName",
    "Identifier",
    "InstructionsModeName",
    "Isolation",
    "JoinPolicy",
    "Money",
    "Name",
    "OnStepFailure",
    "OnUnsupported",
    "PositiveDuration",
    "RunStatus",
    "StepStatus",
    "input_ref_name",
    "parse_duration",
    "parse_money",
    "validate_identifier",
    "validate_name",
]
