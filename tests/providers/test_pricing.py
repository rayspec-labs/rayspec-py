"""Pricing table: lookup precedence, cost math and console formatting."""

from __future__ import annotations

import pytest

from rayspec.errors import RayspecError
from rayspec.providers.base import Usage
from rayspec.providers.pricing import (
    Price,
    PriceTable,
    cost_usd,
    format_cost,
    format_tokens,
)


def _input(table: PriceTable, model: str) -> float:
    price = table.lookup(model)
    assert price is not None
    return price.input


def test_price_is_per_million_and_frozen():
    p = Price(input=2.0, cached_input=0.5, output=8.0)
    assert p.cache_write is None
    with pytest.raises(AttributeError):
        p.input = 3.0  # type: ignore[misc]


def test_from_config_parses_exact_and_globs():
    table = PriceTable.from_config(
        {
            "gpt-5.4": {"input": 1.0, "cached_input": 0.1, "output": 4.0},
            "gpt-5*": {"input": 2.0, "cached_input": 0.5, "output": 8.0},
            "gpt-5.4-mini*": {
                "input": 0.2,
                "cached_input": 0.05,
                "output": 0.8,
                "cache_write": 0.3,
            },
            "claude-opus*": None,
        }
    )
    assert table.lookup("gpt-5.4") == Price(input=1.0, cached_input=0.1, output=4.0)


def test_lookup_exact_beats_glob_and_longest_glob_wins():
    table = PriceTable.from_config(
        {
            "gpt-5*": {"input": 2.0, "cached_input": 0.5, "output": 8.0},
            "gpt-5.4-mini*": {"input": 0.2, "cached_input": 0.05, "output": 0.8},
            "gpt-5.4-mini-2026": {"input": 9.0, "cached_input": 9.0, "output": 9.0},
        }
    )
    assert _input(table, "gpt-5.4-mini-2026") == 9.0  # exact first
    assert _input(table, "gpt-5.4-mini-x") == 0.2  # longest matching glob
    assert _input(table, "gpt-5.4") == 2.0  # shorter glob
    assert table.lookup("claude-sonnet") is None  # no match


def test_null_value_disables_a_model_even_when_a_glob_matches():
    table = PriceTable.from_config(
        {
            "gpt-5*": {"input": 2.0, "cached_input": 0.5, "output": 8.0},
            "gpt-5.4-preview": None,
            "gpt-5.4-experimental*": None,
        }
    )
    assert table.lookup("gpt-5.4-preview") is None
    assert table.lookup("gpt-5.4-experimental-1") is None
    assert table.lookup("gpt-5.4") is not None


def test_lookup_none_model_and_empty_table():
    assert PriceTable.from_config({}).lookup("x") is None
    assert PriceTable.from_config(None).lookup(None) is None
    assert PriceTable().lookup("gpt") is None


def test_from_config_rejects_bad_entries():
    with pytest.raises(RayspecError, match="pricing"):
        PriceTable.from_config({"gpt": {"input": 1.0}})  # missing fields
    with pytest.raises(RayspecError, match="pricing"):
        PriceTable.from_config({"gpt": {"input": "a lot", "cached_input": 1, "output": 1}})
    with pytest.raises(RayspecError, match="pricing"):
        PriceTable.from_config({"gpt": {"input": 1, "cached_input": 1, "output": 1, "bogus": 2}})
    with pytest.raises(RayspecError, match="pricing"):
        PriceTable.from_config({"gpt": [1, 2, 3]})


def test_cost_math_uses_uncached_input_and_cache_write_fallback():
    price = Price(input=2.0, cached_input=0.5, output=8.0)
    usage = Usage(input=1_000_000, cached_input=200_000, cache_write=100_000, output=50_000)
    # uncached = 1_000_000 - 200_000 - 100_000 = 700_000
    expected = (700_000 * 2.0 + 200_000 * 0.5 + 100_000 * 2.0 + 50_000 * 8.0) / 1e6
    assert cost_usd(usage, price) == pytest.approx(expected)
    price_cw = Price(input=2.0, cached_input=0.5, output=8.0, cache_write=3.0)
    expected_cw = (700_000 * 2.0 + 200_000 * 0.5 + 100_000 * 3.0 + 50_000 * 8.0) / 1e6
    assert cost_usd(usage, price_cw) == pytest.approx(expected_cw)


def test_cost_of_empty_usage_is_zero_and_never_negative():
    price = Price(input=2.0, cached_input=0.5, output=8.0)
    assert cost_usd(Usage(), price) == 0.0
    # inconsistent usage (cached > input) must not yield negative cost
    assert cost_usd(Usage(input=10, cached_input=50), price) >= 0.0


def test_table_cost_helper():
    table = PriceTable.from_config({"m*": {"input": 1.0, "cached_input": 0.0, "output": 1.0}})
    assert table.cost_usd("m1", Usage(input=1_000_000, output=1_000_000)) == pytest.approx(2.0)
    assert table.cost_usd("other", Usage(input=10)) is None


@pytest.mark.parametrize(
    ("cost", "source", "usage", "expected"),
    [
        (0.1234, "provider", Usage(input=10), "$0.12"),
        (0.1234, "table", Usage(input=10), "~$0.12"),
        (None, "none", Usage(input=12_000, output=300), "12.3k tok"),
        (None, "provider", Usage(input=850), "850 tok"),
        (None, "none", Usage(input=1_500_000), "1.5M tok"),
        (0.0, "provider", Usage(), "$0.00"),
        (0.0004, "table", Usage(input=1), "~$0.00"),
        (12.5, "provider", Usage(), "$12.50"),
        (None, "none", Usage(), "0 tok"),
    ],
)
def test_format_cost(cost, source, usage, expected):
    assert format_cost(cost, source, usage) == expected


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (0, "0 tok"),
        (999, "999 tok"),
        (1_000, "1.0k tok"),
        (999_949, "999.9k tok"),
        (999_950, "1.0M tok"),  # rounds up into the next unit instead of "1000.0k tok"
        (1_000_000, "1.0M tok"),
    ],
)
def test_format_tokens_rounds_before_choosing_the_unit(total, expected):
    assert format_tokens(total) == expected
