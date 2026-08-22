# SPDX-License-Identifier: Apache-2.0
"""Pricing table: USD-per-million-token prices used when a provider reports no cost.

Boundary: pure data + arithmetic. Depends only on :mod:`rayspec.providers.base` (``Usage``) and
:mod:`rayspec.errors`. The config section ``pricing:`` (``~/.rayspec/config.yaml``) maps a model
id — exact or ``fnmatch`` glob — to a :class:`Price` (or ``null`` to disable pricing for it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from rayspec.errors import RayspecError
from rayspec.providers.base import CostSource, Usage

_GLOB_CHARS = frozenset("*?[")
_PRICE_FIELDS = ("input", "cached_input", "output", "cache_write")
_REQUIRED_FIELDS = ("input", "cached_input", "output")


class PricingConfigError(RayspecError):
    """A ``pricing:`` config entry is malformed."""


@dataclass(frozen=True, slots=True)
class Price:
    """USD per 1M tokens. ``cache_write=None`` bills cache writes at the ``input`` rate."""

    input: float
    cached_input: float
    output: float
    cache_write: float | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, where: str = "pricing") -> Price:
        """Build from a config mapping; raises :class:`PricingConfigError` on bad shape."""
        unknown = sorted(set(data) - set(_PRICE_FIELDS))
        if unknown:
            raise PricingConfigError(
                f"{where}: unknown price field(s) {', '.join(unknown)}; "
                f"allowed: {', '.join(_PRICE_FIELDS)}"
            )
        missing = [k for k in _REQUIRED_FIELDS if k not in data]
        if missing:
            raise PricingConfigError(f"{where}: missing price field(s) {', '.join(missing)}")

        def rate(key: str) -> float:
            raw = data.get(key)
            if isinstance(raw, bool) or not isinstance(raw, int | float) or raw < 0:
                raise PricingConfigError(
                    f"{where}: price field {key!r} must be a non-negative number, got {raw!r}"
                )
            return float(raw)

        cache_write = None if data.get("cache_write") is None else rate("cache_write")
        return cls(
            input=rate("input"),
            cached_input=rate("cached_input"),
            output=rate("output"),
            cache_write=cache_write,
        )


@dataclass(frozen=True, slots=True)
class PriceTable:
    """Model id → :class:`Price` with exact-first, then longest-matching-glob resolution.

    A ``None`` price (``null`` in YAML) *disables* pricing for the matching models even when a
    shorter glob would match.
    """

    exact: Mapping[str, Price | None] = field(default_factory=dict)
    globs: tuple[tuple[str, Price | None], ...] = ()

    @classmethod
    def from_config(cls, mapping: Mapping[str, Any] | None) -> PriceTable:
        """Parse the ``pricing:`` config section (keys: exact model ids or fnmatch globs)."""
        if mapping is None:
            return cls()
        if not isinstance(mapping, Mapping):
            raise PricingConfigError("pricing: must be a mapping of model id/glob -> price")
        exact: dict[str, Price | None] = {}
        globs: list[tuple[str, Price | None]] = []
        for key, raw in mapping.items():
            if not isinstance(key, str) or not key:
                raise PricingConfigError(f"pricing: model key must be a non-empty string: {key!r}")
            if raw is None:
                price: Price | None = None
            elif isinstance(raw, Mapping):
                price = Price.from_mapping(raw, where=f"pricing.{key}")
            else:
                raise PricingConfigError(
                    f"pricing.{key}: expected a mapping with input/cached_input/output or null"
                )
            if _GLOB_CHARS & set(key):
                globs.append((key, price))
            else:
                exact[key] = price
        # longest pattern first → first match wins
        globs.sort(key=lambda item: (-len(item[0]), item[0]))
        return cls(exact=exact, globs=tuple(globs))

    def lookup(self, model: str | None) -> Price | None:
        """Resolve a model id: exact entry, else the longest matching glob, else ``None``."""
        if not model:
            return None
        if model in self.exact:
            return self.exact[model]
        for pattern, price in self.globs:
            if fnmatchcase(model, pattern):
                return price
        return None

    def cost_usd(self, model: str | None, usage: Usage) -> float | None:
        """Convenience: ``cost_usd(usage, self.lookup(model))`` or ``None`` when unpriced."""
        price = self.lookup(model)
        if price is None:
            return None
        return cost_usd(usage, price)


def price_table_of(mapping: Mapping[str, Any] | None) -> tuple[PriceTable, str | None]:
    """``(table, the one line naming a section too malformed to read)`` — never raises.

    The reason this is not a bare ``try``/``except`` at each call site is what the two call sites
    did with it: both caught :class:`PricingConfigError`, dropped the table and said nothing. For
    every provider that reports no cost of its own this table IS the cost, so a section rayspec
    silently discards takes the operator's ``budget:`` envelope with it — the ceilings are then
    compared against a figure that no longer exists, and a run that spent money looks free. That
    is the failure mode this area exists to prevent, and :mod:`rayspec.limits.policy` already
    answers it for a ceiling that cannot be parsed: drop it and NAME it, so an operator never
    believes a guardrail is in force when it is not.

    So the problem is returned rather than raised: the caller prints it beside the run's other
    policy warnings. Nothing here writes to a console.
    """
    try:
        return PriceTable.from_config(mapping), None
    except PricingConfigError as exc:
        return PriceTable(), (
            f"{exc} — the pricing table is not applied, so a step whose provider reports no cost "
            "of its own counts as $0, including against a policy budget: ceiling"
        )


def cost_usd(usage: Usage, price: Price) -> float:
    """USD cost of ``usage`` at ``price``.

    ``uncached = input - cached_input - cache_write`` (clamped at 0); cache writes are billed at
    ``price.cache_write`` or, when unset, at the ``input`` rate.
    """
    uncached = max(usage.input - usage.cached_input - usage.cache_write, 0)
    cache_write_rate = price.cache_write if price.cache_write is not None else price.input
    total = (
        uncached * price.input
        + usage.cached_input * price.cached_input
        + usage.cache_write * cache_write_rate
        + usage.output * price.output
    )
    return max(total, 0.0) / 1e6


def format_tokens(total: int) -> str:
    """``850 tok`` / ``12.3k tok`` / ``1.5M tok``; rounds to one decimal before picking the unit."""
    if total < 1_000:
        return f"{total} tok"
    thousands = round(total / 1e3, 1)
    if thousands < 1_000:
        return f"{thousands:.1f}k tok"
    return f"{round(total / 1e6, 1):.1f}M tok"


#: Run-level cost sources: ``provider`` = every step with tokens reported a provider cost,
#: ``table`` = at least one step cost is a price-table estimate and none is unknown, ``partial`` =
#: at least one step has tokens but no cost at all (unpriced provider, no table entry), ``none`` =
#: no cost anywhere.
COST_SOURCES: tuple[str, ...] = ("provider", "table", "partial", "none")
_COST_MARKERS: dict[str, str] = {"table": "~", "partial": "≥"}


def cost_marker(cost_source: CostSource | str | None) -> str:
    """The prefix every cost rendering uses: ``''`` (provider/none), ``~`` (table), ``≥``
    (partial)."""
    return _COST_MARKERS.get(str(cost_source or ""), "")


def combine_cost_sources(sources: Iterable[str | None], *, unpriced: bool = False) -> str:
    """Fold per-step cost sources into the run-level source (see :data:`COST_SOURCES`).

    ``sources`` are the ``cost_source`` values of the steps that *have* a cost; ``unpriced`` says
    whether any step reported tokens but no cost. ``partial`` wins when both priced and unpriced
    steps exist; ``none`` when nothing is priced; ``table`` when any estimate is in the mix.
    """
    known = {str(s) for s in sources if s and str(s) != "none"}
    if not known:
        return "none"
    if unpriced:
        return "partial"
    return "table" if "table" in known else "provider"


def format_cost(cost_usd: float | None, cost_source: CostSource | str, usage: Usage) -> str:
    """Console rendering: ``$0.12`` (provider), ``~$0.12`` (table), ``≥$0.12`` (partial: some
    steps have tokens but no price), else the token count."""
    if cost_usd is None:
        return format_tokens(usage.total)
    return f"{cost_marker(cost_source)}${cost_usd:.2f}"


__all__ = [
    "COST_SOURCES",
    "Price",
    "PriceTable",
    "PricingConfigError",
    "combine_cost_sources",
    "cost_marker",
    "cost_usd",
    "format_cost",
    "format_tokens",
    "price_table_of",
]
