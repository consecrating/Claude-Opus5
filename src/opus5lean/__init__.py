"""opus5-lean: get the best Claude Opus 5 output per token spent.

Four tools, in the order you should reach for them:

1. :mod:`~opus5lean.tokens`  -- measure. Exact counts via the unbilled
   ``count_tokens`` endpoint, or an offline estimate with no key at all.
2. :mod:`~opus5lean.slim`    -- shrink. Compression passes that never touch
   code blocks, each reporting the tokens it actually saved.
3. :mod:`~opus5lean.cache`   -- amortise. Plan ``cache_control`` breakpoints
   and compute the break-even hit count.
4. :mod:`~opus5lean.sweep`   -- tune. Walk the effort ladder to find the
   cheapest level that still holds quality.

The first three are pure stdlib and run offline. Only :mod:`~opus5lean.sweep`
needs a network call.
"""

from __future__ import annotations

from .cache import CachePlan, plan_cache
from .pricing import (
    DEFAULT_EFFORT,
    EFFORT_LEVELS,
    PRICES,
    CostBreakdown,
    ModelPrice,
    Usage,
    cost,
    price_of,
)
from .slim import SlimResult, slim
from .tokens import CountResult, count_tokens, estimate_tokens

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_EFFORT",
    "EFFORT_LEVELS",
    "PRICES",
    "CachePlan",
    "CostBreakdown",
    "CountResult",
    "ModelPrice",
    "SlimResult",
    "Usage",
    "__version__",
    "cost",
    "count_tokens",
    "estimate_tokens",
    "plan_cache",
    "price_of",
    "slim",
]
