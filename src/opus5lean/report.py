"""Terminal reporting. Plain ASCII tables, no dependencies.

Reports here follow one rule: never present an estimate as a measurement.
Estimated token counts are prefixed with ``~`` and labelled, so a number you
could bill against is always visually distinct from one you couldn't.
"""

from __future__ import annotations

from collections.abc import Sequence

from .cache import CachePlan
from .pricing import MILLION, Usage, cost, fmt_usd, price_of
from .slim import SlimResult
from .sweep import SweepResult
from .tokens import CountResult


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], indent: str = "") -> str:
    """Render an ASCII table, numeric-looking columns right-aligned."""
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(row[i])) if i < len(row) else 0)

    def is_num(i: int) -> bool:
        vals = [str(r[i]) for r in rows if i < len(r) and str(r[i]).strip()]
        if not vals:
            return False
        return all(v.lstrip("~$+-").replace(",", "").replace(".", "").replace("%", "")
                   .replace("s", "").isdigit() or v == "-" for v in vals)

    aligns = [is_num(i) for i in range(cols)]

    def fmt_row(cells: Sequence[str]) -> str:
        out = []
        for i in range(cols):
            cell = str(cells[i]) if i < len(cells) else ""
            out.append(cell.rjust(widths[i]) if aligns[i] else cell.ljust(widths[i]))
        return indent + "  ".join(out).rstrip()

    # Don't draw a rule under an unlabelled column -- it reads as a missing header.
    sep = indent + "  ".join(("-" * w if headers[i] else " " * w) for i, w in enumerate(widths))
    lines = [fmt_row(headers), sep]
    lines.extend(fmt_row(r) for r in rows)
    return "\n".join(lines)


def bar(fraction: float, width: int = 24) -> str:
    """A simple filled bar for proportions."""
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "#" * filled + "." * (width - filled)


def render_count(result: CountResult, *, label: str = "input") -> str:
    p = price_of(result.model)
    as_input = result.tokens / MILLION * p.input
    as_output = result.tokens / MILLION * p.output
    mark = "" if result.is_exact else "~"

    lines = [f"{label}: {mark}{result.tokens:,} tokens  [{result.method}]  model={result.model}"]
    if result.detail:
        d = result.detail
        chars = d.get("chars", 0)
        ratio = chars / result.tokens if result.tokens else 0
        lines.append(
            f"  {chars:,} chars ({ratio:.2f} chars/token)"
            + (f", {d['code_chars']:,} in code blocks" if d.get("code_chars") else "")
        )
    lines.append(
        f"  cost if sent as input:  {fmt_usd(as_input)}"
        f"   |  as output: {fmt_usd(as_output)}"
    )
    lines.append(
        f"  context used: {result.tokens / p.context_window * 100:.2f}% "
        f"of {p.context_window:,}  {bar(result.tokens / p.context_window)}"
    )
    if not result.is_exact:
        lines.append("  note: estimate (typically within ~10-15%). Use --exact to confirm.")
    return "\n".join(lines)


def render_slim(result: SlimResult, *, model: str = "claude-opus-5", requests: int = 1) -> str:
    lines = ["Compression", ""]
    mark = "" if result.before.is_exact else "~"
    lines.append(
        f"  {mark}{result.before.tokens:,} -> {mark}{result.after.tokens:,} tokens"
        f"   saved {result.saved:,} ({result.pct:.1f}%)"
    )

    effective = result.effective_passes
    if effective:
        lines += ["", "  Where the savings came from:", ""]
        rows = [
            [p.name, f"{p.saved:,}", f"{p.saved / result.before.tokens * 100:.1f}%", p.why]
            for p in effective
        ]
        lines.append(table(["pass", "saved", "of total", "what it does"], rows, indent="    "))

    dead = [p.name for p in result.passes if p.saved <= 0]
    if dead:
        lines += ["", f"  No effect on this text: {', '.join(dead)}"]

    if result.saved > 0:
        per_req = cost(Usage(input_tokens=result.saved), model).total
        lines += [
            "",
            f"  Saves {fmt_usd(per_req)} per request on {model}"
            f"  |  {fmt_usd(per_req * requests):>10} over {requests:,} requests"
            f"  |  {fmt_usd(per_req * 1_000_000)} per million",
        ]
    return "\n".join(lines)


def render_cache_plan(plan: CachePlan, *, requests: int = 100) -> str:
    p = price_of(plan.model)
    lines = [f"Cache plan  ({plan.model}, {plan.ttl} TTL)", "", f"  {plan.summary()}", ""]

    rows = []
    cumulative = 0
    bp_at = {b.index for b in plan.breakpoints}
    for i, seg in enumerate(plan.segments):
        cumulative += seg.tokens
        rows.append(
            [
                seg.name,
                f"{seg.tokens:,}",
                f"{cumulative:,}",
                seg.label,
                "<== cache_control" if i in bp_at else "",
            ]
        )
    lines.append(table(["segment", "tokens", "cumulative", "volatility", ""], rows, indent="    "))

    lines += ["", f"  Minimum cacheable prefix for {plan.model}: {p.cache_min_tokens:,} tokens"]
    lines.append(
        f"  Rates per MTok -- input {fmt_usd(p.input)}, "
        f"write {fmt_usd(p.cache_write_1h if plan.ttl == '1h' else p.cache_write_5m)}, "
        f"hit {fmt_usd(p.cache_hit)} ({p.cache_hit_discount:.0%} of input)"
    )
    lines.append(f"  Break-even: {plan.break_even_hits} cache hit(s)")

    lines += ["", "  Projection:", ""]
    rows = []
    for n in sorted({1, 10, 100, requests, 1000, 10_000}):
        rows.append(
            [
                f"{n:,}",
                fmt_usd(plan.cost_uncached(n)),
                fmt_usd(plan.cost_cached(n)),
                fmt_usd(plan.savings(n)),
                f"{plan.savings_pct(n):.0f}%",
            ]
        )
    lines.append(
        table(["requests", "no cache", "cached", "saved", "saved %"], rows, indent="    ")
    )

    if plan.warnings:
        lines += ["", "  Warnings:"]
        lines += [f"    ! {w}" for w in plan.warnings]
    if plan.suggested_order and [s.name for s in plan.segments] != plan.suggested_order:
        lines += [
            "",
            "  Suggested send order (most stable first):",
            "    " + " -> ".join(plan.suggested_order),
        ]
    return "\n".join(lines)


def render_sweep(result: SweepResult) -> str:
    lines = [f"Effort sweep  ({result.model})", ""]
    rows = []
    for lv in result.levels:
        if not lv.ok:
            rows.append([lv.effort, "-", "-", "-", "-", "error"])
            continue
        rows.append(
            [
                lv.effort,
                f"{lv.mean_score:.2f}" if result.graded else "-",
                f"{lv.mean_output:,.0f}",
                fmt_usd(lv.mean_cost),
                f"{lv.mean_latency:.1f}s",
                "PASS" if result.graded and lv.mean_score >= result.threshold else "",
            ]
        )
    lines.append(
        table(["effort", "score", "out tok", "cost/req", "latency", ""], rows, indent="    ")
    )

    if not result.graded:
        lines += [
            "",
            "  Ungraded: no recommendation. Cost alone cannot tell you whether a",
            "  cheaper level is good enough -- supply --require to gate on quality.",
        ]
        return "\n".join(lines)

    rec = result.recommended
    if not rec:
        lines += [
            "",
            f"  No level reached the {result.threshold:.2f} threshold. Either the task",
            "  needs more than max effort, or the grader is too strict.",
        ]
        return "\n".join(lines)

    lines += ["", f"  Recommended: effort={rec}"]
    saved = result.savings_vs_baseline()
    if saved is not None:
        base = result.baseline
        assert base is not None
        if saved > 0.01:
            lines.append(
                f"  {saved:.0%} cheaper than the default (high): "
                f"{fmt_usd(result.by_effort[rec].mean_cost)} vs "
                f"{fmt_usd(base.mean_cost)} per request"
            )
        else:
            lines.append("  The default (high) is already the cheapest passing level.")
    return "\n".join(lines)
