# SPDX-License-Identifier: Apache-2.0
"""Pricing-coverage helper shared by ``rayspec plan`` and ``rayspec doctor``.

Private module (underscore → not auto-registered as a command). Boundary: pure presentation
logic over :class:`rayspec.config.Config` and :mod:`rayspec.providers.pricing` — no SDK imports,
no provider instantiation. It answers one question: for a provider that reports no USD cost,
which of the models a run would use have a price entry (so cost shows as ``~$``) and which would
only ever show a token count?
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from rayspec.cli._docs import docs_url
from rayspec.config import TIER_NAMES, Config
from rayspec.errors import RayspecError
from rayspec.providers.pricing import Price, PriceTable

#: URL of the pricing section in the provider docs (quoted in hints; a full link).
PRICING_DOCS = docs_url("docs/providers.md#pricing")


@dataclass(frozen=True, slots=True)
class PricingCoverage:
    """Which of ``models`` are priced for one provider.

    ``priced`` models resolve to a price (``~$`` estimates); ``disabled`` models match a ``null``
    entry (the user opted out — tokens only, no nudge); ``unpriced`` models match nothing (tokens
    only — the nudge names them). ``error`` is set when a ``pricing:`` table is malformed: a broken
    per-provider table prices nothing (the adapter refuses it), a broken global table is skipped
    and the per-provider table still applies (the engine's fallback is what the run would drop).
    ``configured`` says whether any pricing entry exists at all (the difference between "never
    set up" and "set up but not matching").
    """

    priced: list[str] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    configured: bool = False
    error: str | None = None

    @property
    def complete(self) -> bool:
        """Every model has a price (and there is at least one model); a broken *global* table
        does not change that when the per-provider table covers everything."""
        return bool(self.priced) and not self.unpriced and not self.disabled


def _resolve(table: PriceTable, model: str) -> tuple[bool, Price | None]:
    """``(matched, price)`` with the table's exact-first, longest-glob resolution — unlike
    :meth:`PriceTable.lookup` it tells a ``null`` entry (matched, ``None``) from no match."""
    if model in table.exact:
        return True, table.exact[model]
    for pattern, price in table.globs:
        if fnmatchcase(model, pattern):
            return True, price
    return False, None


def pricing_coverage(config: Config, provider_id: str, models: Iterable[str]) -> PricingCoverage:
    """Resolve ``models`` against the per-provider table (``providers.<id>.pricing``), then the
    global ``pricing:`` table — the order the engine uses (adapter estimate first, engine fallback
    second; a ``null`` in the adapter table still lets the global table price the model). Models
    are de-duplicated, order preserved.
    """
    ordered = list(dict.fromkeys(m for m in models if m))
    provider_raw = config.providers.get(provider_id, {}).get("pricing")
    configured = bool(provider_raw) or bool(config.pricing)
    try:
        provider_table = PriceTable.from_config(provider_raw)
    except RayspecError as exc:
        return PricingCoverage(unpriced=ordered, configured=True, error=str(exc))
    error: str | None = None
    try:
        global_table = PriceTable.from_config(config.pricing or None)
    except RayspecError as exc:
        global_table, error = PriceTable(), str(exc)
    priced: list[str] = []
    unpriced: list[str] = []
    disabled: list[str] = []
    for model in ordered:
        matched, price = _resolve(provider_table, model)
        if price is None:
            matched_global, price = _resolve(global_table, model)
            matched = matched or matched_global
        if price is not None:
            priced.append(model)
        elif matched:
            disabled.append(model)
        else:
            unpriced.append(model)
    return PricingCoverage(
        priced=priced, unpriced=unpriced, disabled=disabled, configured=configured, error=error
    )


def configured_models(config: Config, provider_id: str) -> list[str]:
    """The models a run on ``provider_id`` may resolve to from config alone: its tier models
    (config, then the built-in defaults) and the ``@alias`` entries that belong to it — aliases
    pinned with ``provider:`` and, for ``config.default_provider`` only, the unpinned ones. (At
    run time an unpinned alias takes the *agent's* provider, which config alone cannot know; an
    unpinned alias used by an agent of another provider is therefore not covered here —
    ``rayspec plan`` sees the resolved workflow and reports it.)"""
    models: list[str] = []
    for tier in TIER_NAMES:
        spec = config.resolve_tier(provider_id, tier)
        if spec is not None:
            models.append(spec.model)
    for alias in config.aliases.values():
        if (alias.provider or config.default_provider) == provider_id:
            models.append(alias.model)
    return list(dict.fromkeys(models))


def nudge(unpriced: Iterable[str]) -> str:
    """``add pricing.<model> for estimates (<docs url>#pricing)`` for the given models."""
    keys = " / ".join(f"pricing.{m}" for m in unpriced) or "pricing.<model>"
    return f"add {keys} for estimates ({PRICING_DOCS})"


def describe(coverage: PricingCoverage) -> str:
    """The human one-liner shared by ``plan`` and ``doctor``: ``estimated from the pricing table
    (~$)[ for …]`` / ``pricing disabled (null) for …`` / ``tokens only[ for …] — add pricing.<model>
    …`` / ``pricing table invalid: …``, joined with ``;``. A broken table that prices nothing is
    reported alone (repairing it is the fix, not adding entries)."""
    if coverage.error is not None and not coverage.priced and not coverage.disabled:
        return f"tokens only — pricing table invalid: {coverage.error}"
    parts: list[str] = []
    if coverage.priced:
        parts.append(f"estimated from the pricing table (~$) for {', '.join(coverage.priced)}")
    if coverage.disabled:
        parts.append(f"pricing disabled (null) for {', '.join(coverage.disabled)}")
    if coverage.unpriced:
        # name the unpriced models only next to priced/disabled ones (the nudge names them anyway)
        scope = f" for {', '.join(coverage.unpriced)}" if parts else ""
        parts.append(f"tokens only{scope} — {nudge(coverage.unpriced)}")
    if coverage.error is not None:
        parts.append(f"pricing table invalid: {coverage.error}")
    if not parts:  # no model at all (e.g. every agent resolved to the provider default)
        return f"tokens only — {nudge(())}"
    return "; ".join(parts)


__all__ = [
    "PRICING_DOCS",
    "PricingCoverage",
    "configured_models",
    "describe",
    "nudge",
    "pricing_coverage",
]
