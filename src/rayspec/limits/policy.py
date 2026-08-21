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

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rayspec.limits.envelope import BudgetEnvelope, RunEnvelope
from rayspec.limits.ledger import SpendLedger, ledger_path
from rayspec.limits.slots import WAIT_FOREVER
from rayspec.loader.loader import import_optional

#: Where the policy layer is expected to live and what is called on it.
POLICY_MODULE = "rayspec.policy"
POLICY_LOADER = "load_policy"


@dataclass(frozen=True, slots=True)
class LimitsPolicy:
    """The narrow view of the policy this layer acts on.

    ``warnings`` names every ceiling that was written but could not be read. A ceiling nobody
    can parse must be visible: dropping one silently is how an operator ends up believing a
    control is in force when it is not. The CLI prints them before the run starts.
    """

    budget: BudgetEnvelope = field(default_factory=BudgetEnvelope)
    max_concurrent_runs: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

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
    problems: list[str] = []
    return LimitsPolicy(
        budget=budget_envelope(
            _get(policy, "budget"), _get(policy, "max_consecutive_failures"), problems=problems
        ),
        max_concurrent_runs=concurrency_limits(
            _get(policy, "max_concurrent_runs"), problems=problems
        ),
        warnings=tuple(problems),
    )


def budget_envelope(
    budget: Any, max_consecutive_failures: Any = None, *, problems: list[str] | None = None
) -> BudgetEnvelope:
    """``policy.budget`` (+ the top-level failure cap) as a :class:`BudgetEnvelope`.

    ``max_consecutive_failures`` is also accepted inside ``budget:`` — the top-level spelling
    wins when both are present. Unreadable values are dropped and named in ``problems``.
    """
    failures = _count(max_consecutive_failures, "max_consecutive_failures", problems)
    if failures is None:
        failures = _count(
            _get(budget, "max_consecutive_failures"), "budget.max_consecutive_failures", problems
        )
    return BudgetEnvelope(
        per_run=_amount(_get(budget, "per_run"), "budget.per_run", problems),
        per_day=_amount(_get(budget, "per_day"), "budget.per_day", problems),
        per_month=_amount(_get(budget, "per_month"), "budget.per_month", problems),
        max_consecutive_failures=failures,
    )


def concurrency_limits(value: Any, *, problems: list[str] | None = None) -> dict[str, int]:
    """``{provider: limit}`` from a mapping, or ``{"*": n}`` from a bare integer.

    ``0`` is a real limit (this provider may not run), not the absence of one.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        limit = _count(value, "max_concurrent_runs", problems)
        return {} if limit is None else {"*": limit}
    out: dict[str, int] = {}
    for key, raw in value.items():
        parsed = _count(raw, f"max_concurrent_runs.{key}", problems)
        if parsed is not None:
            out[str(key)] = parsed
    return out


def limits_for(limits: Mapping[str, int], providers: Any) -> dict[str, int]:
    """The per-provider limits that apply to ``providers`` (``"*"`` covers every one of them).

    A limit of ``0`` is kept: it means the provider may not run on this host at all.
    """
    default = limits.get("*")
    out: dict[str, int] = {}
    for provider in providers:
        limit = limits.get(provider, default)
        if limit is not None:
            out[provider] = int(limit)
    return out


def workflow_providers(resolved: Any) -> list[str]:
    """The provider ids the ``prompt:`` steps of a workflow resolve to, sorted and de-duplicated.

    A workflow without prompt steps needs no provider and therefore no run slot.
    """
    agents = getattr(resolved, "agents", {})
    keys = getattr(resolved, "step_agents", {}).values()
    return sorted({agents[key].provider for key in keys if key in agents})


def run_envelope(
    policy: LimitsPolicy,
    *,
    store_root: Path,
    run_id: str,
) -> RunEnvelope | None:
    """The live envelope for one run, or ``None`` when nothing is capped.

    The ledger lives beside the project's runs (``<store root>/limits/spend.json``): per user,
    per machine, per project — the same scope the run store itself has.
    """
    if not policy.budget.active:
        return None
    return RunEnvelope(
        policy.budget,
        SpendLedger(ledger_path(store_root)),
        run_id=run_id,
    )


def wait_seconds(value: str | None) -> float | None:
    """``--wait-slot`` as :meth:`RunSlot.acquire`'s ``wait_s`` (``None`` = do not wait).

    :data:`~rayspec.limits.slots.WAIT_FOREVER` (``forever``) is the ONLY indefinite spelling
    (``math.inf``); anything else is a duration in the workflow vocabulary (``30m``, ``90``,
    ``1h30m``). ``0`` means "do not wait", the way it reads everywhere else — never "wait for
    ever" — and a negative duration is a usage error (:class:`ValueError`).
    """
    if value is None:
        return None
    text = value.strip()
    if text.lower() == WAIT_FOREVER:
        return math.inf
    from rayspec.schema import parse_duration

    try:
        seconds = float(text)  # a bare number of seconds ("90"), which parse_duration rejects
    except ValueError:
        seconds = parse_duration(text)
    if seconds < 0:
        raise ValueError(f"{text!r} is negative — pass a duration, `0` or `forever`")
    return float(seconds) or None


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _note(problems: list[str] | None, key: str, value: Any, why: str) -> None:
    if problems is not None:
        problems.append(f"policy {key}: {value!r} {why} — the ceiling is not applied")


def _number(value: Any, key: str, problems: list[str] | None) -> float | None:
    """A ceiling as a number: ``None``/absent means no ceiling, ``0`` means zero.

    Accepts what YAML and a JSON round-trip produce for the same idea — ``20``, ``20.0``,
    ``"20"`` — so two spellings of one number never behave oppositely. A boolean is not a
    number, and a negative ceiling is meaningless.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        _note(problems, key, value, "is not a number")
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        _note(problems, key, value, "is not a number")
        return None
    if math.isnan(number) or number < 0:
        _note(problems, key, value, "must not be negative")
        return None
    return number


def _amount(value: Any, key: str, problems: list[str] | None = None) -> float | None:
    """A money ceiling in USD (``0`` = spend nothing)."""
    return _number(value, key, problems)


def _count(value: Any, key: str, problems: list[str] | None = None) -> int | None:
    """A whole-number ceiling (``0`` = none at all)."""
    number = _number(value, key, problems)
    if number is None:
        return None
    if number != int(number):
        _note(problems, key, value, "must be a whole number")
        return None
    return int(number)


__all__ = [
    "POLICY_LOADER",
    "POLICY_MODULE",
    "LimitsPolicy",
    "budget_envelope",
    "concurrency_limits",
    "limits_for",
    "limits_policy",
    "policy_view",
    "run_envelope",
    "wait_seconds",
    "workflow_providers",
]
