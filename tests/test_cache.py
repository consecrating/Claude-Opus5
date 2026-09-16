"""Cache breakpoint planning and break-even economics."""

from __future__ import annotations

import pytest

from opus5lean.cache import (
    MAX_BREAKPOINTS,
    CachePlan,
    Segment,
    break_even_requests,
    monthly_savings,
    plan_cache,
)

# A typical agent prompt: static tools, static system, per-session context,
# per-turn conversation.
TYPICAL = [
    Segment("tool schemas", 4000, volatility=0),
    Segment("system prompt", 6000, volatility=0),
    Segment("retrieved docs", 20000, volatility=1),
    Segment("session context", 1500, volatility=2),
    Segment("conversation", 800, volatility=3),
]


def test_break_even_is_under_one_hit_on_5m():
    """1.25x write premium against a 10x read discount pays back immediately."""
    assert plan_cache(TYPICAL).break_even_hits == 1


def test_break_even_is_two_hits_on_1h():
    """The 1h TTL costs 2x to write, so it needs a second hit."""
    assert plan_cache(TYPICAL, ttl="1h").break_even_hits == 2


def test_break_even_requests_helper():
    assert break_even_requests("claude-opus-5", "5m") == 2
    assert break_even_requests("claude-opus-5", "1h") == 3


def test_breakpoints_land_where_volatility_rises():
    plan = plan_cache(TYPICAL)
    assert [b.after_segment for b in plan.breakpoints] == [
        "system prompt",   # end of the static run
        "retrieved docs",  # end of the slow-changing run
        "session context",  # end of the per-session run
    ]


def test_cached_tokens_follow_the_last_breakpoint():
    plan = plan_cache(TYPICAL)
    assert plan.total_tokens == 32300
    assert plan.cached_tokens == 31500  # everything but the conversation
    assert plan.uncached_tokens == 800


def test_breakpoints_are_returned_in_send_order():
    plan = plan_cache(TYPICAL)
    assert [b.index for b in plan.breakpoints] == sorted(b.index for b in plan.breakpoints)


def test_caching_beats_no_caching_once_reused():
    plan = plan_cache(TYPICAL)
    assert plan.cost_cached(1) > plan.cost_uncached(1)   # the write premium
    assert plan.cost_cached(2) < plan.cost_uncached(2)   # already ahead
    assert plan.savings_pct(1000) > 80                   # ~10x on the prefix


def test_misordered_prompt_is_flagged():
    """A volatile block early in the prompt kills the whole prefix."""
    plan = plan_cache([
        Segment("timestamp", 20, volatility=3),
        Segment("system prompt", 9000, volatility=0),
    ])
    assert any("ordering problem" in w for w in plan.warnings)
    assert "'system prompt'" in " ".join(plan.warnings)
    assert plan.suggested_order == ["system prompt", "timestamp"]


def test_well_ordered_prompt_has_no_ordering_warning():
    plan = plan_cache(TYPICAL)
    assert not any("ordering problem" in w for w in plan.warnings)


def test_prompt_below_the_minimum_is_reported_not_cached():
    plan = plan_cache([
        Segment("tiny system", 100, volatility=0),
        Segment("turn", 50, volatility=3),
    ])
    assert plan.breakpoints == []
    assert any("needs 512" in w for w in plan.warnings)


def test_opus5_lower_minimum_enables_prompts_opus48_could_not_cache():
    """512 vs 1024 -- a real behaviour difference, not just a number."""
    segs = [Segment("system", 700, volatility=0), Segment("turn", 100, volatility=3)]
    assert plan_cache(segs, model="claude-opus-5").breakpoints != []
    assert plan_cache(segs, model="claude-opus-4-8").breakpoints == []


def test_breakpoints_are_capped_and_largest_win():
    segs = [Segment(f"s{i}", 1000, volatility=i) for i in range(8)]
    plan = plan_cache(segs)
    assert len(plan.breakpoints) == MAX_BREAKPOINTS
    assert any("allows 4" in w for w in plan.warnings)
    # The kept breakpoints should be the deepest ones.
    assert plan.cached_tokens == 7000


def test_uniform_static_prompt_caches_all_but_the_last_segment():
    segs = [Segment("a", 5000), Segment("b", 5000), Segment("c", 5000)]
    plan = plan_cache(segs)
    assert plan.cached_tokens == 10000


def test_all_volatile_prompt_is_reported_as_uncacheable():
    segs = [Segment(f"turn{i}", 5000, volatility=3) for i in range(3)]
    plan = plan_cache(segs)
    assert plan.breakpoints == []
    assert any("equally volatile" in w for w in plan.warnings)


def test_context_window_overflow_is_flagged():
    plan = plan_cache(
        [Segment("huge", 300_000, 0), Segment("turn", 10, 3)], model="claude-opus-4-8"
    )
    assert any("tops out" in w for w in plan.warnings)


def test_empty_segments():
    plan = plan_cache([])
    assert plan.total_tokens == 0
    assert plan.warnings == ["no segments given"]
    assert plan.savings_pct(10) == 0.0


def test_bad_ttl_rejected():
    with pytest.raises(ValueError, match="5m|1h"):
        plan_cache(TYPICAL, ttl="1d")


def test_summary_mentions_the_key_numbers():
    s = plan_cache(TYPICAL).summary()
    assert "31,500" in s and "32,300" in s and "break-even" in s


def test_monthly_projection_saves_money():
    m = monthly_savings(30_000, 800, requests_per_day=5_000)
    assert m["requests"] == 150_000
    assert m["with_cache"] < m["without_cache"]
    assert 80 < m["saved_pct"] < 100


def test_monthly_projection_scales_linearly_with_traffic():
    a = monthly_savings(30_000, 800, 1_000, writes_per_day=0)
    b = monthly_savings(30_000, 800, 2_000, writes_per_day=0)
    assert b["saved"] == pytest.approx(a["saved"] * 2, rel=1e-6)


def test_zero_requests_costs_nothing():
    assert CachePlan("claude-opus-5", "5m", list(TYPICAL)).cost_cached(0) == 0.0
