"""Prompt-cache breakpoint planning.

Prompt caching is the largest single lever on Claude cost, and it is
underused because the economics are non-obvious. On Opus 5:

* a base input token costs $5/MTok,
* writing it into the 5-minute cache costs $6.25/MTok (a 1.25x premium),
* reading it back costs $0.50/MTok (a 10x discount).

So a cached prefix pays for itself after **less than one** reuse on the 5m TTL,
and after two on the 1h TTL. Anyone sending a stable system prompt more than
once and not caching it is overpaying by roughly 10x on that prefix.

The subtlety is that the cache matches on *prefix*. One volatile block placed
early -- a timestamp, a session id, a per-turn variable -- invalidates
everything after it, and the usual symptom is a cache that silently never hits.
:func:`plan_cache` finds that class of bug by ordering your prompt segments by
how often they change and reporting where the ordering breaks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .pricing import MILLION, Usage, cost, price_of

#: The API accepts at most four ``cache_control`` breakpoints per request.
MAX_BREAKPOINTS = 4

#: Volatility tiers. Lower changes less often and belongs earlier in the prompt.
VOLATILITY_LABELS = {
    0: "static (never changes)",
    1: "slow (changes on deploy)",
    2: "per-session",
    3: "per-turn",
}


@dataclass
class Segment:
    """One contiguous piece of a prompt, in the order it is sent.

    Args:
        name: Label for reports, e.g. ``"tool schemas"``.
        tokens: Size of this segment.
        volatility: How often it changes. 0 = static, 1 = per deploy,
            2 = per session, 3 = per turn. Segments should be ordered
            ascending; anything else breaks prefix caching.
    """

    name: str
    tokens: int
    volatility: int = 0

    @property
    def label(self) -> str:
        return VOLATILITY_LABELS.get(self.volatility, f"tier {self.volatility}")


@dataclass
class Breakpoint:
    """A recommended ``cache_control`` placement."""

    after_segment: str
    index: int
    cached_tokens: int

    def __str__(self) -> str:
        return f"after {self.after_segment!r} ({self.cached_tokens:,} tokens cached)"


@dataclass
class CachePlan:
    """Result of planning: where to cache, what it saves, what's wrong."""

    model: str
    ttl: str
    segments: list[Segment]
    breakpoints: list[Breakpoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggested_order: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(s.tokens for s in self.segments)

    @property
    def cached_tokens(self) -> int:
        """Tokens covered by the furthest breakpoint."""
        return self.breakpoints[-1].cached_tokens if self.breakpoints else 0

    @property
    def uncached_tokens(self) -> int:
        return self.total_tokens - self.cached_tokens

    # -- economics ---------------------------------------------------------

    def cost_uncached(self, requests: int = 1) -> float:
        """Cost of sending the whole prompt ``requests`` times with no cache."""
        return cost(Usage(input_tokens=self.total_tokens * requests), self.model).total

    def cost_cached(self, requests: int = 1) -> float:
        """Cost of one cache write followed by ``requests - 1`` cache hits."""
        if requests < 1:
            return 0.0
        first = Usage(
            input_tokens=self.uncached_tokens,
            cache_write_tokens=self.cached_tokens,
            cache_ttl=self.ttl,
        )
        total = cost(first, self.model).total
        if requests > 1:
            rest = Usage(
                input_tokens=self.uncached_tokens * (requests - 1),
                cache_read_tokens=self.cached_tokens * (requests - 1),
                cache_ttl=self.ttl,
            )
            total += cost(rest, self.model).total
        return total

    def savings(self, requests: int) -> float:
        return self.cost_uncached(requests) - self.cost_cached(requests)

    def savings_pct(self, requests: int) -> float:
        base = self.cost_uncached(requests)
        return (self.savings(requests) / base * 100) if base else 0.0

    @property
    def break_even_hits(self) -> int:
        """Cache hits needed before caching is cheaper than not caching.

        Solves ``W + h*H <= (1 + h)*I`` for the smallest whole ``h``, where
        ``W`` is the write rate, ``H`` the hit rate and ``I`` the base input
        rate. Returns 0 when caching wins immediately.
        """
        p = price_of(self.model)
        write = p.cache_write_1h if self.ttl == "1h" else p.cache_write_5m
        # Per cached token: write premium is (W - I); each hit saves (I - H).
        premium = write - p.input
        per_hit_saving = p.input - p.cache_hit
        if per_hit_saving <= 0:  # pragma: no cover - not true for any real model
            return 10**9
        h = 0
        while premium > h * per_hit_saving:
            h += 1
        return h

    def summary(self) -> str:
        pct = self.cached_tokens / self.total_tokens * 100 if self.total_tokens else 0
        return (
            f"{self.cached_tokens:,}/{self.total_tokens:,} tokens cacheable "
            f"({pct:.0f}%), {len(self.breakpoints)} breakpoint(s), "
            f"break-even after {self.break_even_hits} hit(s)"
        )


def plan_cache(
    segments: list[Segment],
    *,
    model: str = "claude-opus-5",
    ttl: str = "5m",
) -> CachePlan:
    """Choose ``cache_control`` breakpoints for an ordered prompt.

    Breakpoints are placed where volatility increases: that is the boundary
    past which content stops being reusable. Where more than
    :data:`MAX_BREAKPOINTS` such boundaries exist, the ones covering the most
    tokens win.

    Args:
        segments: Prompt pieces in send order.
        model: Price-table key; determines the minimum cacheable prefix.
        ttl: ``"5m"`` or ``"1h"``.

    Returns:
        A :class:`CachePlan` with breakpoints, warnings and cost projections.
    """
    if ttl not in ("5m", "1h"):
        raise ValueError("ttl must be '5m' or '1h'")
    plan = CachePlan(model=model, ttl=ttl, segments=list(segments))
    if not segments:
        plan.warnings.append("no segments given")
        return plan

    p = price_of(model)
    minimum = p.cache_min_tokens

    # -- ordering lint: a prefix cache is only as long as its first volatile block
    worst = 0
    misordered: list[str] = []
    for seg in segments:
        if seg.volatility < worst:
            misordered.append(seg.name)
        worst = max(worst, seg.volatility)
    if misordered:
        plan.warnings.append(
            "prefix-cache ordering problem: "
            + ", ".join(f"{n!r}" for n in misordered)
            + " appear after more volatile content, so they can never be cached. "
            "Move stable content to the front of the prompt."
        )
    # Sort by volatility only. Python's sort is stable, so segments within a
    # tier keep their original order -- reordering them would change nothing
    # about caching, and suggesting it would be noise.
    plan.suggested_order = [s.name for s in sorted(segments, key=lambda s: s.volatility)]

    # -- candidate boundaries: end of each volatility run
    cumulative = 0
    candidates: list[Breakpoint] = []
    for i, seg in enumerate(segments):
        cumulative += seg.tokens
        is_last = i == len(segments) - 1
        next_more_volatile = not is_last and segments[i + 1].volatility > seg.volatility
        if next_more_volatile and cumulative >= minimum:
            candidates.append(Breakpoint(seg.name, i, cumulative))

    if not candidates:
        uniform = all(s.volatility == segments[0].volatility for s in segments)
        stable = sum(s.tokens for s in segments if s.volatility <= 1)
        if uniform and segments[0].volatility >= 2:
            # Everything changes every turn or session: nothing to reuse.
            plan.warnings.append(
                "every segment is equally volatile, so there is no stable "
                "prefix to cache. Split out whatever does not change."
            )
        elif uniform and len(segments) > 1:
            # Uniformly stable: cache everything except the final segment,
            # which is where new content will land.
            cum = sum(s.tokens for s in segments[:-1])
            if cum >= minimum:
                candidates.append(Breakpoint(segments[-2].name, len(segments) - 2, cum))
            else:
                plan.warnings.append(
                    f"only {cum:,} cacheable tokens; {model} needs {minimum:,}. "
                    "Consolidate static context or skip caching here."
                )
        elif stable < minimum:
            plan.warnings.append(
                f"only {stable:,} stable tokens; {model} needs {minimum:,} to cache "
                "anything. Consolidate static context or skip caching here."
            )

    # Keep the breakpoints that cover the most tokens, then restore send order.
    chosen = sorted(candidates, key=lambda b: -b.cached_tokens)[:MAX_BREAKPOINTS]
    plan.breakpoints = sorted(chosen, key=lambda b: b.index)

    if len(candidates) > MAX_BREAKPOINTS:
        plan.warnings.append(
            f"{len(candidates)} useful boundaries but the API allows "
            f"{MAX_BREAKPOINTS}; kept the {MAX_BREAKPOINTS} largest."
        )
    if plan.cached_tokens and plan.uncached_tokens > plan.cached_tokens:
        plan.warnings.append(
            f"{plan.uncached_tokens:,} tokens sit after the last breakpoint and are "
            "billed at full rate every request; consider whether some of that is "
            "actually stable."
        )
    if plan.total_tokens > p.context_window:
        plan.warnings.append(
            f"prompt is {plan.total_tokens:,} tokens but {model} tops out at "
            f"{p.context_window:,}."
        )
    return plan


def break_even_requests(model: str = "claude-opus-5", ttl: str = "5m") -> int:
    """Total requests (write + hits) at which caching turns profitable."""
    plan = CachePlan(model=model, ttl=ttl, segments=[Segment("x", 1)])
    return plan.break_even_hits + 1


def monthly_savings(
    cached_tokens: int,
    uncached_tokens: int,
    requests_per_day: int,
    *,
    model: str = "claude-opus-5",
    ttl: str = "5m",
    writes_per_day: int = 24,
) -> dict[str, float]:
    """Project a month of spend with and without caching.

    ``writes_per_day`` is how often the cache entry has to be re-written --
    with a 5-minute TTL, roughly once per idle gap, so hourly (24) is a
    reasonable default for a service with steady traffic.
    """
    p = price_of(model)
    days = 30
    reqs = requests_per_day * days
    writes = min(writes_per_day * days, reqs)
    hits = max(reqs - writes, 0)

    write_rate = p.cache_write_1h if ttl == "1h" else p.cache_write_5m
    with_cache = (
        cached_tokens * writes / MILLION * write_rate
        + cached_tokens * hits / MILLION * p.cache_hit
        + uncached_tokens * reqs / MILLION * p.input
    )
    without = (cached_tokens + uncached_tokens) * reqs / MILLION * p.input
    return {
        "requests": float(reqs),
        "with_cache": with_cache,
        "without_cache": without,
        "saved": without - with_cache,
        "saved_pct": ((without - with_cache) / without * 100) if without else 0.0,
    }
