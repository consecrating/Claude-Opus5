"""Pricing table and cost math."""

from __future__ import annotations

import pytest

from opus5lean.pricing import (
    EFFORT_LEVELS,
    MILLION,
    PRICES,
    Usage,
    cost,
    fmt_usd,
    price_of,
    resolve,
)


def test_opus5_published_rates():
    p = price_of("claude-opus-5")
    assert (p.input, p.output) == (5.00, 25.00)
    assert (p.cache_write_5m, p.cache_write_1h, p.cache_hit) == (6.25, 10.00, 0.50)
    assert p.cache_min_tokens == 512
    assert p.context_window == 1_000_000
    assert p.max_output == 128_000


def test_cache_multipliers_match_documented_shape():
    """Writes are 1.25x input; hits are 0.1x -- except Fable/Mythos 5.1 at 0.025x."""
    for name, p in PRICES.items():
        assert p.cache_write_5m == pytest.approx(p.input * 1.25), name
        assert p.cache_write_1h == pytest.approx(p.input * 2.0), name
        expected = 0.025 if name.endswith("5-1") else 0.1
        assert p.cache_hit == pytest.approx(p.input * expected), name


def test_aliases_resolve():
    for alias in ("opus-5", "opus5", "OPUS-5", " Opus5 "):
        assert resolve(alias) == "claude-opus-5"
    assert resolve("claude-haiku-4-5-20251001") == "claude-haiku-4-5"


def test_unknown_model_lists_options():
    with pytest.raises(KeyError, match="claude-opus-5"):
        price_of("gpt-9")


def test_cost_is_linear_and_itemised():
    c = cost(Usage(input_tokens=MILLION, output_tokens=MILLION), "claude-opus-5")
    assert c.input == pytest.approx(5.0)
    assert c.output == pytest.approx(25.0)
    assert c.total == pytest.approx(30.0)


def test_cache_read_is_ten_times_cheaper_than_input():
    read = cost(Usage(cache_read_tokens=MILLION), "claude-opus-5").total
    plain = cost(Usage(input_tokens=MILLION), "claude-opus-5").total
    assert plain / read == pytest.approx(10.0)


def test_ttl_changes_write_price():
    u5 = Usage(cache_write_tokens=MILLION, cache_ttl="5m")
    u1 = Usage(cache_write_tokens=MILLION, cache_ttl="1h")
    assert cost(u5).total == pytest.approx(6.25)
    assert cost(u1).total == pytest.approx(10.0)


def test_effort_ladder_is_cheapest_first():
    assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")


def test_fmt_usd_keeps_sub_cent_amounts_visible():
    assert fmt_usd(0) == "$0"
    assert fmt_usd(0.000004) == "$0.000004"   # would render as $0.00 at 2dp
    assert fmt_usd(0.5) == "$0.5000"
    assert fmt_usd(1234.5) == "$1,234.50"
