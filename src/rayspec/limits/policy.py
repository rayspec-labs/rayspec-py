# SPDX-License-Identifier: Apache-2.0
"""Reading the two policy keys this layer consumes — and nothing else.

Module boundary: the policy FILE (its schema, its layering, its loader) has one owner,
:mod:`rayspec.policy`. This module is a *consumer*: it asks that module for the effective
policy and narrows it to the two things operational limits need —

* ``policy.budget`` → :class:`~rayspec.limits.envelope.BudgetEnvelope`
  (``per_run`` / ``per_day`` / ``per_month``), plus ``policy.max_consecutive_failures``;
* ``policy.max_concurrent_runs`` → ``{provider id: limit}`` (a bare integer applies to every
  provider).

It reads through the accessor rather than the file so the layering rules stay in one place, and
it degrades to "no limits" when the policy layer is not importable — an operational ceiling that
cannot be read must not break a run that never asked for one.

Local only: nothing here fetches a policy from a server, and there is no organisation id.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rayspec.limits.envelope import BudgetEnvelope
from rayspec.loader.loader import import_optional

#: Where the policy layer is expected to live and what is called on it.
POLICY_MODULE = "rayspec.policy"
POLICY_LOADER = "load_policy"


@dataclass(frozen=True, slots=True)
class LimitsPolicy:
    """The narrow view of the policy this layer acts on."""

    budget: BudgetEnvelope = field(default_factory=BudgetEnvelope)
    max_concurrent_runs: Mapping[str, int] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        """Whether anything at all is limited."""
        return self.budget.active or bool(self.max_concurrent_runs)


def limits_policy(
    project_root: Path, *, home: Path, environ: Mapping[str, str] | None = None
) -> LimitsPolicy:
    """The effective limits for ``project_root`` (empty when no policy layer/file applies)."""
    module = import_optional(POLICY_MODULE)
    loader = getattr(module, POLICY_LOADER, None) if module is not None else None
    if loader is None:
        return LimitsPolicy()
    try:
        policy = loader(project_root, home=home, environ=environ)
    except TypeError:  # a loader with a different signature — treat as absent, never crash a run
        return LimitsPolicy()
    return policy_view(policy)


def policy_view(policy: Any) -> LimitsPolicy:
    """Narrow a loaded policy object (or plain mapping) to :class:`LimitsPolicy`."""
    if policy is None:
        return LimitsPolicy()
    return LimitsPolicy(
        budget=budget_envelope(_get(policy, "budget"), _get(policy, "max_consecutive_failures")),
        max_concurrent_runs=concurrency_limits(_get(policy, "max_concurrent_runs")),
    )


def budget_envelope(budget: Any, max_consecutive_failures: Any = None) -> BudgetEnvelope:
    """``policy.budget`` (+ the top-level failure cap) as a :class:`BudgetEnvelope`.

    ``max_consecutive_failures`` is also accepted inside ``budget:`` — the top-level spelling
    wins when both are present.
    """
    failures = _positive_int(max_consecutive_failures)
    if failures is None:
        failures = _positive_int(_get(budget, "max_consecutive_failures"))
    return BudgetEnvelope(
        per_run=_positive_float(_get(budget, "per_run")),
        per_day=_positive_float(_get(budget, "per_day")),
        per_month=_positive_float(_get(budget, "per_month")),
        max_consecutive_failures=failures,
    )


def concurrency_limits(value: Any) -> dict[str, int]:
    """``{provider: limit}`` from a mapping, or ``{"*": n}`` from a bare integer."""
    if value is None:
        return {}
    limit = _positive_int(value)
    if limit is not None:
        return {"*": limit}
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, int] = {}
    for key, raw in value.items():
        parsed = _positive_int(raw)
        if parsed is not None:
            out[str(key)] = parsed
    return out


def limits_for(limits: Mapping[str, int], providers: Any) -> dict[str, int]:
    """The per-provider limits that apply to ``providers`` (``"*"`` covers every one of them)."""
    default = limits.get("*")
    out: dict[str, int] = {}
    for provider in providers:
        limit = limits.get(provider, default)
        if limit:
            out[provider] = int(limit)
    return out


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _positive_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool | float | str):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


__all__ = [
    "POLICY_LOADER",
    "POLICY_MODULE",
    "LimitsPolicy",
    "budget_envelope",
    "concurrency_limits",
    "limits_for",
    "limits_policy",
    "policy_view",
]
