"""Effort sweeps: find the cheapest effort level that still holds quality.

Anthropic's own guidance for Opus 5 is to treat ``low`` and ``medium`` as the
primary cost control and to step down wherever evals show quality holds -- and
to re-sweep rather than reuse effort settings carried over from an earlier
model. This module is that sweep.

The method is deliberately boring, because the failure mode here is fooling
yourself:

1. Run the same task at each effort level, cheapest first.
2. Score each result with a grader you supply. A grader that always returns
   1.0 measures nothing, so the default is an explicit "ungraded" mode that
   refuses to recommend a level.
3. Report the cheapest level whose score clears your threshold.

What comes out is a number you can defend: "medium passes 10/10 of our checks
at 38% of the cost of high."
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .client import AnthropicClient, ApiError, Response
from .pricing import EFFORT_LEVELS, cost, fmt_usd

#: A grader maps a response's text to a score in [0, 1].
Grader = Callable[[str], float]


def substring_grader(required: Sequence[str], *, forbidden: Sequence[str] = ()) -> Grader:
    """Build a grader from must-appear and must-not-appear substrings.

    Crude but deterministic and free, which makes it a reasonable first gate
    before you invest in a real eval.
    """
    req = [r.lower() for r in required]
    bad = [f.lower() for f in forbidden]

    def grade(text: str) -> float:
        low = text.lower()
        if any(f in low for f in bad):
            return 0.0
        if not req:
            return 1.0
        return sum(1 for r in req if r in low) / len(req)

    return grade


@dataclass
class LevelResult:
    """Aggregate of all trials at one effort level."""

    effort: str
    scores: list[float] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    output_tokens: list[int] = field(default_factory=list)
    input_tokens: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    responses: list[Response] = field(default_factory=list)

    @property
    def trials(self) -> int:
        return len(self.responses)

    @property
    def mean_score(self) -> float:
        return statistics.fmean(self.scores) if self.scores else 0.0

    @property
    def mean_cost(self) -> float:
        return statistics.fmean(self.costs) if self.costs else 0.0

    @property
    def mean_latency(self) -> float:
        return statistics.fmean(self.latencies) if self.latencies else 0.0

    @property
    def mean_output(self) -> float:
        return statistics.fmean(self.output_tokens) if self.output_tokens else 0.0

    @property
    def ok(self) -> bool:
        return self.trials > 0 and not self.errors


@dataclass
class SweepResult:
    """Full sweep across effort levels, plus the recommendation."""

    model: str
    levels: list[LevelResult]
    threshold: float
    graded: bool

    @property
    def by_effort(self) -> dict[str, LevelResult]:
        return {lv.effort: lv for lv in self.levels}

    @property
    def recommended(self) -> str | None:
        """Cheapest level clearing ``threshold``.

        ``None`` when the sweep was ungraded (nothing to compare) or when no
        level passed.
        """
        if not self.graded:
            return None
        for lv in self.levels:  # already cheapest-first
            if lv.ok and lv.mean_score >= self.threshold:
                return lv.effort
        return None

    @property
    def baseline(self) -> LevelResult | None:
        """The ``high`` level, which is what you get without setting effort."""
        return self.by_effort.get("high")

    def savings_vs_baseline(self) -> float | None:
        """Fractional cost reduction from the recommendation vs. the default."""
        rec, base = self.recommended, self.baseline
        if not rec or not base or not base.mean_cost:
            return None
        return 1 - (self.by_effort[rec].mean_cost / base.mean_cost)


def sweep_effort(
    prompt: str,
    *,
    model: str = "claude-opus-5",
    system: str | None = None,
    grader: Grader | None = None,
    threshold: float = 1.0,
    levels: Sequence[str] = EFFORT_LEVELS,
    trials: int = 1,
    max_tokens: int = 8192,
    client: AnthropicClient | None = None,
    verbose: bool = True,
) -> SweepResult:
    """Run ``prompt`` at each effort level and measure cost, latency and score.

    Args:
        grader: Scores each response in [0, 1]. Without one the sweep still
            reports cost and latency but makes no recommendation -- choosing
            an effort level on cost alone is how quality regressions ship.
        threshold: Minimum mean score for a level to be acceptable.
        trials: Repeats per level. Output length varies run to run, so >1 is
            worth it before acting on small differences.
        levels: Which levels to try, cheapest first.
    """
    cli = client or AnthropicClient()
    graded = grader is not None
    results: list[LevelResult] = []

    for effort in levels:
        lv = LevelResult(effort=effort)
        for _ in range(trials):
            try:
                resp = cli.complete(
                    prompt,
                    model=model,
                    system=system,
                    effort=effort,
                    max_tokens=max_tokens,
                )
            except ApiError as exc:
                lv.errors.append(str(exc))
                continue
            lv.responses.append(resp)
            lv.costs.append(cost(resp.usage, model).total)
            lv.latencies.append(resp.latency_s)
            lv.output_tokens.append(resp.usage.output_tokens)
            lv.input_tokens.append(resp.usage.input_tokens)
            if grader:
                lv.scores.append(max(0.0, min(1.0, grader(resp.text))))
        results.append(lv)
        if verbose:
            print(_line(lv, graded))

    return SweepResult(model=model, levels=results, threshold=threshold, graded=graded)


def _line(lv: LevelResult, graded: bool) -> str:
    if lv.errors and not lv.responses:
        return f"  {lv.effort:<7} error: {lv.errors[0][:90]}"
    score = f"score {lv.mean_score:.2f}  " if graded else ""
    return (
        f"  {lv.effort:<7} {score}"
        f"out {lv.mean_output:>7,.0f} tok  "
        f"{fmt_usd(lv.mean_cost):>12}  "
        f"{lv.mean_latency:>6.1f}s"
    )
