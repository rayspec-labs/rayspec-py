# SPDX-License-Identifier: Apache-2.0
"""The ``policy.yaml`` document model — guardrails as data.

Boundary: pure Pydantic models plus the ordering of access levels. Nothing here reads a file,
knows about workflows or talks to a provider; :mod:`rayspec.policy.layers` loads these documents
and :mod:`rayspec.policy.enforce` applies them to a resolved workflow.

Every block is *restrictive only*: a key can forbid something a workflow would otherwise be
allowed to do, and there is no key that grants a permission. That is what makes layering safe —
see :mod:`rayspec.policy.layers` for the most-restrictive-wins merge.

The document is the union of every key a shipped page documents and a shipped module reads, not
just the ones this package enforces itself: ``budget:``, ``max_consecutive_failures:`` and
``max_concurrent_runs:`` belong to :mod:`rayspec.limits`, which reads them through
:func:`~rayspec.policy.layers.load_policy`. A strict model that rejected them would have made
every documented operational limit a hard load failure, so
``tests/policy/test_policy_document.py`` parses the policy blocks of ``docs/`` against this model
and checks the keys the limits layer reads against its fields.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from rayspec.schema.base import StrictModel
from rayspec.schema.common import AccessLevelName

#: A whole-number ceiling: ``0`` is a real ceiling (none at all), a negative one is meaningless.
Count = Annotated[int, Field(ge=0)]

#: A money ceiling in USD; ``0`` means "spend nothing".
Amount = Annotated[float, Field(ge=0)]

#: Access levels from least to most powerful; the index is the level's rank.
ACCESS_ORDER: tuple[str, ...] = ("read-only", "workspace-write", "full")


def access_rank(level: str) -> int:
    """Rank of an access level (higher = more powerful); unknown levels rank highest."""
    try:
        return ACCESS_ORDER.index(level)
    except ValueError:
        return len(ACCESS_ORDER)


class ProvidersPolicy(StrictModel):
    """``providers.allow`` — the provider ids agents may resolve to (``None`` = unrestricted)."""

    allow: list[str] | None = None

    @classmethod
    def _what(cls) -> str:
        return "providers policy"


class ModelsPolicy(StrictModel):
    """``models.deny`` — model ids or globs (``*opus*``) no agent may use."""

    deny: list[str] = Field(default_factory=list)

    @classmethod
    def _what(cls) -> str:
        return "models policy"


class AccessPolicy(StrictModel):
    """``access.max`` — the most powerful sandbox level an agent may ask for."""

    max: AccessLevelName | None = None

    @classmethod
    def _what(cls) -> str:
        return "access policy"


class ToolsPolicy(StrictModel):
    """``tools.deny`` — neutral tool entries (``web``, ``shell``, ``mcp:github``) kept away."""

    deny: list[str] = Field(default_factory=list)

    @classmethod
    def _what(cls) -> str:
        return "tools policy"


class McpPolicy(StrictModel):
    """``mcp.allow_servers`` — the MCP server names agents may declare (``None`` = any)."""

    allow_servers: list[str] | None = None

    @classmethod
    def _what(cls) -> str:
        return "mcp policy"


class WorkspacePolicy(StrictModel):
    """The worktree change guard: paths that must not change and how much may change at all."""

    protected_paths: list[str] = Field(default_factory=list)
    max_changed_files: int | None = Field(default=None, ge=0)
    max_changed_lines: int | None = Field(default=None, ge=0)

    @classmethod
    def _what(cls) -> str:
        return "workspace policy"


class BudgetPolicy(StrictModel):
    """``budget:`` — the operator's spending envelope, and the failure breaker beside it.

    These are the ceilings :mod:`rayspec.limits` reads. Reaching one is not a defect the way a
    workflow author's own ``budget_usd`` is: it is the moment the machine was supposed to stop and
    ask, so the run PAUSES instead of failing. ``max_consecutive_failures`` may be spelled here or
    at the top level of the document; the top-level spelling wins when both are set.
    """

    per_run: Amount | None = None
    per_day: Amount | None = None
    per_month: Amount | None = None
    max_consecutive_failures: Count | None = None

    @classmethod
    def _what(cls) -> str:
        return "budget policy"


class TrustPolicy(StrictModel):
    """``trust.require`` — only workflows listed in ``.rayspec/trusted.yaml`` may run."""

    require: bool = False

    @classmethod
    def _what(cls) -> str:
        return "trust policy"


class Policy(StrictModel):
    """One ``policy.yaml`` document (one layer).

    A key that is not set imposes nothing; that is how a layer stays additive-in-restriction. New
    blocks are added here and given a merge rule in :mod:`rayspec.policy.layers` — a key without a
    merge rule would let a lower layer widen a higher one.
    """

    providers: ProvidersPolicy = Field(default_factory=ProvidersPolicy)
    models: ModelsPolicy = Field(default_factory=ModelsPolicy)
    access: AccessPolicy = Field(default_factory=AccessPolicy)
    tools: ToolsPolicy = Field(default_factory=ToolsPolicy)
    mcp: McpPolicy = Field(default_factory=McpPolicy)
    workspace: WorkspacePolicy = Field(default_factory=WorkspacePolicy)
    trust: TrustPolicy = Field(default_factory=TrustPolicy)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    max_consecutive_failures: Count | None = None
    max_concurrent_runs: Count | dict[str, Count] | None = None

    @classmethod
    def _what(cls) -> str:
        return "policy"


__all__ = [
    "ACCESS_ORDER",
    "AccessPolicy",
    "Amount",
    "BudgetPolicy",
    "Count",
    "McpPolicy",
    "ModelsPolicy",
    "Policy",
    "ProvidersPolicy",
    "ToolsPolicy",
    "TrustPolicy",
    "WorkspacePolicy",
    "access_rank",
]
