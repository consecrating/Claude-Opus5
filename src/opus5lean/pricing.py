"""Price table and cost math.

All rates are USD per million tokens (MTok), transcribed from Anthropic's
pricing / prompt-caching docs. See docs/TOKEN-PLAYBOOK.md for provenance and
the date this table was last verified.

Every number a user sees comes from here. There are no magic constants
scattered through the codebase, so re-verifying prices is a one-file job.
"""

from __future__ import annotations

from dataclasses import dataclass

MILLION = 1_000_000

# Effort ladder, cheapest first. Order matters: `sweep` walks it in this
# direction so the first level that holds quality is also the cheapest.
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: Effort level the API applies when you don't set one.
DEFAULT_EFFORT = "high"


@dataclass(frozen=True)
class ModelPrice:
    """Per-MTok rates for one model.

    Attributes:
        input: Base (uncached) input tokens.
        cache_write_5m: Writing a cache entry with the default 5-minute TTL.
        cache_write_1h: Writing a cache entry with the extended 1-hour TTL.
        cache_hit: Reading from cache (also called a cache refresh).
        output: Output tokens. Thinking tokens bill at this rate too.
        cache_min_tokens: A prefix shorter than this cannot be cached at all.
        context_window: Maximum total tokens in one request.
        max_output: Hard cap on output, thinking included.
    """

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_hit: float
    output: float
    cache_min_tokens: int
    context_window: int
    max_output: int

    @property
    def cache_write_premium(self) -> float:
        """How much more a 5m cache write costs than a plain input token."""
        return self.cache_write_5m / self.input

    @property
    def cache_hit_discount(self) -> float:
        """Cache-hit rate as a fraction of the base input rate."""
        return self.cache_hit / self.input


# Verified against platform.claude.com pricing + prompt-caching docs, 2026-09.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5": ModelPrice(
        input=5.00,
        cache_write_5m=6.25,
        cache_write_1h=10.00,
        cache_hit=0.50,
        output=25.00,
        cache_min_tokens=512,  # lowered from 1024 on Opus 4.8
        context_window=1_000_000,
        max_output=128_000,
    ),
    # Opus 5 "fast mode" (research preview, Claude API only) -- same model,
    # premium rates for higher output tokens/sec. Caching rates are not
    # published separately; base input/output are what you actually pay.
    "claude-opus-5-fast": ModelPrice(
        input=10.00,
        cache_write_5m=12.50,
        cache_write_1h=20.00,
        cache_hit=1.00,
        output=50.00,
        cache_min_tokens=512,
        context_window=1_000_000,
        max_output=128_000,
    ),
    "claude-opus-4-8": ModelPrice(
        input=5.00,
        cache_write_5m=6.25,
        cache_write_1h=10.00,
        cache_hit=0.50,
        output=25.00,
        cache_min_tokens=1024,
        context_window=200_000,
        max_output=64_000,
    ),
    "claude-sonnet-5": ModelPrice(
        input=2.00,
        cache_write_5m=2.50,
        cache_write_1h=4.00,
        cache_hit=0.20,
        output=10.00,
        cache_min_tokens=1024,
        context_window=200_000,
        max_output=64_000,
    ),
    "claude-haiku-4-5": ModelPrice(
        input=1.00,
        cache_write_5m=1.25,
        cache_write_1h=2.00,
        cache_hit=0.10,
        output=5.00,
        cache_min_tokens=2048,
        context_window=200_000,
        max_output=64_000,
    ),
    "claude-fable-5": ModelPrice(
        input=10.00,
        cache_write_5m=12.50,
        cache_write_1h=20.00,
        cache_hit=1.00,
        output=50.00,
        cache_min_tokens=1024,
        context_window=200_000,
        max_output=64_000,
    ),
    # Fable/Mythos 5.1 price cache hits at 0.025x base rather than the usual 0.1x.
    "claude-fable-5-1": ModelPrice(
        input=10.00,
        cache_write_5m=12.50,
        cache_write_1h=20.00,
        cache_hit=0.25,
        output=50.00,
        cache_min_tokens=1024,
        context_window=200_000,
        max_output=64_000,
    ),
}

# Convenience spellings -> canonical keys.
ALIASES: dict[str, str] = {
    "opus-5": "claude-opus-5",
    "opus5": "claude-opus-5",
    "opus-5-fast": "claude-opus-5-fast",
    "opus-4.8": "claude-opus-4-8",
    "claude-opus-4.8": "claude-opus-4-8",
    "sonnet-5": "claude-sonnet-5",
    "haiku-4.5": "claude-haiku-4-5",
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "fable-5": "claude-fable-5",
    "fable-5.1": "claude-fable-5-1",
    "claude-fable-5.1": "claude-fable-5-1",
}


def resolve(model: str) -> str:
    """Normalise a model name to a key in :data:`PRICES`."""
    key = model.strip().lower()
    key = ALIASES.get(key, key)
    if key not in PRICES:
        known = ", ".join(sorted(PRICES))
        raise KeyError(f"unknown model {model!r}; known models: {known}")
    return key


def price_of(model: str) -> ModelPrice:
    """Look up rates for ``model``, accepting aliases."""
    return PRICES[resolve(model)]


@dataclass(frozen=True)
class Usage:
    """Token counts for a single request, split by how each part is billed."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cache_ttl: str = "5m"

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_write_tokens
            + self.cache_read_tokens
        )


@dataclass(frozen=True)
class CostBreakdown:
    """Dollar cost of a :class:`Usage`, itemised so you can see the driver."""

    input: float
    output: float
    cache_write: float
    cache_read: float

    @property
    def total(self) -> float:
        return self.input + self.output + self.cache_write + self.cache_read

    def as_dict(self) -> dict[str, float]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_write": self.cache_write,
            "cache_read": self.cache_read,
            "total": self.total,
        }


def cost(usage: Usage, model: str = "claude-opus-5") -> CostBreakdown:
    """Price a :class:`Usage` against ``model``'s rates.

    Thinking tokens are not a separate line item: they bill as output tokens,
    so fold them into ``usage.output_tokens``.
    """
    p = price_of(model)
    write_rate = p.cache_write_1h if usage.cache_ttl == "1h" else p.cache_write_5m
    return CostBreakdown(
        input=usage.input_tokens / MILLION * p.input,
        output=usage.output_tokens / MILLION * p.output,
        cache_write=usage.cache_write_tokens / MILLION * write_rate,
        cache_read=usage.cache_read_tokens / MILLION * p.cache_hit,
    )


def fmt_usd(amount: float) -> str:
    """Format a dollar amount with enough precision to stay meaningful.

    Sub-cent figures are common when pricing a single request, and rounding
    them to 2dp would print a misleading ``$0.00``.
    """
    if amount == 0:
        return "$0"
    if abs(amount) < 0.01:
        return f"${amount:.6f}"
    if abs(amount) < 1:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"
