# SPDX-License-Identifier: Apache-2.0
"""The workflow document model."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BeforeValidator,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from rayspec.schema.agent import AgentDef
from rayspec.schema.base import StrictModel
from rayspec.schema.common import (
    Duration,
    Identifier,
    Isolation,
    Name,
    OnStepFailure,
    OnUnsupported,
    PositiveDuration,
)
from rayspec.schema.errors import schema_error_from_validation
from rayspec.schema.inputs import InputSpec
from rayspec.schema.steps import Step, StepModel, iter_steps

SCHEMA_VERSION = 1

_TOKEN_COUNT_RE = re.compile(r"^\s*(\d+(?:_\d+)*(?:\.\d+)?)\s*([kKmM])?\s*$")
_TOKEN_COUNT_HINT = "use a whole number of tokens (e.g. 500000) or a string like '500k', '1.5M'"
_MONEY_RE = re.compile(r"^\s*\$?\s*(\d+(?:\.\d+)?)\s*(?:USD)?\s*$", re.IGNORECASE)
_MONEY_HINT = "use a USD amount (e.g. 1.5) or a string like '$1.50'"


def parse_token_count(value: object) -> int:
    """Parse a token cap into an int (``1500``, ``"500k"``, ``"1.5M"``); Duration-like."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid token count {value!r}; {_TOKEN_COUNT_HINT}")
    if isinstance(value, int):
        count = value
    elif isinstance(value, str):
        match = _TOKEN_COUNT_RE.match(value)
        if match is None:
            raise ValueError(f"invalid token count {value!r}; {_TOKEN_COUNT_HINT}")
        number, unit = match.groups()
        scale = {"k": 1_000, "m": 1_000_000}.get((unit or "").lower(), 1)
        exact = float(number.replace("_", "")) * scale
        if not exact.is_integer():
            raise ValueError(
                f"invalid token count {value!r}: not a whole number of tokens; {_TOKEN_COUNT_HINT}"
            )
        count = int(exact)
    else:
        raise ValueError(f"invalid token count {value!r}; {_TOKEN_COUNT_HINT}")
    if count <= 0:
        raise ValueError("must be greater than 0")
    return count


def parse_money(value: object) -> float:
    """Parse a USD cap into a float (``1.5``, ``"1.50"``, ``"$1.50"``, ``"12 USD"``)."""
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


#: ``defaults.max_tokens``: positive int; accepts ``500k`` / ``1.5M`` strings.
TokenCount = Annotated[int, BeforeValidator(parse_token_count)]
#: ``defaults.budget_usd``: positive USD amount; accepts ``"$1.50"`` strings.
Money = Annotated[float, BeforeValidator(parse_money)]


class Defaults(StrictModel):
    agent: str | None = None
    timeout: Duration | None = None
    max_parallel: int = Field(default=4, ge=1)
    on_unsupported: OnUnsupported = "error"
    on_step_failure: OnStepFailure = "drain"
    #: run-level circuit breaker: once the run's total cost (provider-reported or
    #: pricing-table estimate) exceeds it, no new leaf starts, running ones finish, the run ends
    #: ``failed`` with reason ``budget exceeded (…)``; ``None`` = no cap
    budget_usd: Money | None = None
    #: same for the run's total tokens (input + output), always enforceable
    max_tokens: TokenCount | None = None
    #: same for the run's wall-clock duration, measured from the run's ORIGINAL start (a resume
    #: keeps counting); ``None`` = no cap. Not a per-step timeout — see ``timeout``.
    timeout_total: PositiveDuration | None = None

    @classmethod
    def _what(cls) -> str:
        return "defaults"


class Workflow(StrictModel):
    rayspec: Any = Field(description="schema version (must be 1)")
    name: Identifier
    description: str = ""
    inputs: dict[Name, InputSpec] = Field(default_factory=dict)
    defaults: Defaults = Field(default_factory=Defaults)
    isolation: Isolation = "worktree"
    agents: dict[Name, AgentDef] = Field(default_factory=dict)
    steps: list[Step]
    #: values are deep-rendered: str = template, dict/list recursed, other scalars pass through
    outputs: dict[Name, Any] = Field(default_factory=dict)

    @classmethod
    def _what(cls) -> str:
        return "workflow"

    @field_validator("rayspec")
    @classmethod
    def _schema_version(cls, value: Any) -> Literal[1]:
        if (value is not SCHEMA_VERSION and value != SCHEMA_VERSION) or isinstance(value, bool):
            raise ValueError(
                f"unsupported schema version {value!r}; this rayspec understands "
                f"'rayspec: {SCHEMA_VERSION}'"
            )
        return SCHEMA_VERSION

    @model_validator(mode="after")
    def _unique_step_ids(self) -> Workflow:
        seen: dict[str, str] = {}
        for path, step in iter_steps(self.all_steps()):
            if step.id in seen:
                raise ValueError(
                    f"duplicate step id {step.id!r} (at {seen[step.id]} and {path}); "
                    "step ids must be unique within a workflow file"
                )
            seen[step.id] = path
        return self

    def all_steps(self) -> list[StepModel]:
        return list(self.steps)


def parse_workflow(data: Any, *, source: str | None = None) -> Workflow:
    try:
        return Workflow.model_validate(data)
    except ValidationError as exc:
        raise schema_error_from_validation(exc, data, source=source) from None


__all__ = [
    "SCHEMA_VERSION",
    "Defaults",
    "Money",
    "TokenCount",
    "Workflow",
    "parse_money",
    "parse_token_count",
    "parse_workflow",
]
