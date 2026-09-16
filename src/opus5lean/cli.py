"""Command-line interface.

    opus5-lean count   FILE        exact or estimated token count + cost
    opus5-lean slim    FILE        compress a prompt, show per-pass savings
    opus5-lean cache   PLAN.json   plan cache_control breakpoints
    opus5-lean sweep   FILE        find the cheapest effort level that holds
    opus5-lean cost                price a token count
    opus5-lean models               show the price table

Everything except ``sweep`` runs offline. ``count --exact`` calls the unbilled
``count_tokens`` endpoint, so it needs a key but costs nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .cache import Segment, monthly_savings, plan_cache
from .pricing import EFFORT_LEVELS, PRICES, Usage, cost, fmt_usd, price_of, resolve
from .report import render_cache_plan, render_count, render_slim, render_sweep, table
from .slim import PASSES, slim
from .tokens import count


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"error: no such file: {path}")
    return p.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_count(args: argparse.Namespace) -> int:
    text = _read(args.file)
    result = count(text, model=args.model, exact=args.exact)
    if args.json:
        print(json.dumps({
            "tokens": result.tokens,
            "method": result.method,
            "model": result.model,
            **result.detail,
        }, indent=2))
    else:
        print(render_count(result, label=Path(args.file).name if args.file != "-" else "stdin"))
    return 0


def cmd_slim(args: argparse.Namespace) -> int:
    text = _read(args.file)
    result = slim(
        text,
        aggressive=args.aggressive,
        only=args.only.split(",") if args.only else None,
        exclude=args.exclude.split(",") if args.exclude else None,
        model=args.model,
        exact=args.exact,
    )
    if args.output:
        Path(args.output).write_text(result.slimmed, encoding="utf-8")
    if args.stdout:
        sys.stdout.write(result.slimmed)
        return 0

    print(render_slim(result, model=args.model, requests=args.requests))
    if args.output:
        print(f"\n  wrote {args.output}")
    elif not args.stdout:
        print("\n  (dry run -- pass -o FILE to write, or --stdout to pipe)")

    if args.min_saving and result.pct < args.min_saving:
        print(f"\n  FAIL: saved {result.pct:.1f}%, required {args.min_saving:.1f}%")
        return 1
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    """Plan breakpoints from a JSON segment spec.

    Spec format::

        [{"name": "tool schemas", "tokens": 3200, "volatility": 0}, ...]

    or ``{"segments": [...], "model": "...", "ttl": "5m"}``. ``tokens`` may be
    replaced by ``file`` or ``text``, which are counted for you.
    """
    raw = json.loads(_read(args.file))
    spec = raw if isinstance(raw, dict) else {"segments": raw}
    segments: list[Segment] = []
    for entry in spec.get("segments", []):
        tokens = entry.get("tokens")
        if tokens is None:
            if "file" in entry:
                body = _read(entry["file"])
            elif "text" in entry:
                body = entry["text"]
            else:
                raise SystemExit(f"error: segment {entry.get('name')!r} needs tokens/file/text")
            tokens = count(body, model=args.model, exact=args.exact).tokens
        segments.append(
            Segment(
                name=entry.get("name", f"segment {len(segments) + 1}"),
                tokens=int(tokens),
                volatility=int(entry.get("volatility", 0)),
            )
        )

    model = spec.get("model", args.model)
    ttl = spec.get("ttl", args.ttl)
    plan = plan_cache(segments, model=model, ttl=ttl)

    if args.json:
        print(json.dumps({
            "model": plan.model,
            "ttl": plan.ttl,
            "total_tokens": plan.total_tokens,
            "cached_tokens": plan.cached_tokens,
            "uncached_tokens": plan.uncached_tokens,
            "break_even_hits": plan.break_even_hits,
            "breakpoints": [
                {"after": b.after_segment, "index": b.index, "cached_tokens": b.cached_tokens}
                for b in plan.breakpoints
            ],
            "warnings": plan.warnings,
            "suggested_order": plan.suggested_order,
            "savings_at_1000_requests": plan.savings(1000),
        }, indent=2))
        return 0

    print(render_cache_plan(plan, requests=args.requests))
    if args.rpd:
        m = monthly_savings(
            plan.cached_tokens, plan.uncached_tokens, args.rpd, model=model, ttl=ttl
        )
        print(
            f"\n  At {args.rpd:,} requests/day for 30 days ({m['requests']:,.0f} requests):"
            f"\n    without cache  {fmt_usd(m['without_cache'])}"
            f"\n    with cache     {fmt_usd(m['with_cache'])}"
            f"\n    saved          {fmt_usd(m['saved'])}  ({m['saved_pct']:.0f}%)"
        )
    return 0 if not plan.warnings or not args.strict else 1


def cmd_sweep(args: argparse.Namespace) -> int:
    from .client import AnthropicClient, ApiError
    from .sweep import substring_grader, sweep_effort

    # Validate configuration and credentials before touching stdin or files:
    # a sweep that reads a large prompt and then dies on a missing key wastes
    # the operator's time, and "-" would block on a terminal.
    levels = args.levels.split(",") if args.levels else list(EFFORT_LEVELS)
    bad = set(levels) - set(EFFORT_LEVELS)
    if bad:
        raise SystemExit(f"error: unknown effort level(s): {', '.join(sorted(bad))}")

    try:
        client = AnthropicClient()
    except ApiError as exc:
        raise SystemExit(
            f"error: {exc}\n"
            "sweep needs a paid Anthropic key -- it makes real completions.\n"
            "Offline commands (count/slim/cache/cost) work without one."
        ) from exc

    text = _read(args.file)
    system = _read(args.system) if args.system else None
    grader = None
    if args.require or args.forbid:
        grader = substring_grader(
            args.require.split(",") if args.require else [],
            forbidden=args.forbid.split(",") if args.forbid else [],
        )

    print(f"Sweeping {args.model} over {', '.join(levels)} x{args.trials} trial(s)...\n")
    result = sweep_effort(
        text,
        model=args.model,
        system=system,
        grader=grader,
        threshold=args.threshold,
        levels=levels,
        trials=args.trials,
        max_tokens=args.max_tokens,
        client=client,
        verbose=False,
    )
    print(render_sweep(result))
    if args.save:
        Path(args.save).write_text(json.dumps({
            "model": result.model,
            "threshold": result.threshold,
            "graded": result.graded,
            "recommended": result.recommended,
            "levels": [
                {
                    "effort": lv.effort,
                    "trials": lv.trials,
                    "mean_score": lv.mean_score,
                    "mean_cost": lv.mean_cost,
                    "mean_latency": lv.mean_latency,
                    "mean_output_tokens": lv.mean_output,
                    "errors": lv.errors,
                }
                for lv in result.levels
            ],
        }, indent=2), encoding="utf-8")
        print(f"\n  wrote {args.save}")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    usage = Usage(
        input_tokens=args.input,
        output_tokens=args.output,
        cache_write_tokens=args.cache_write,
        cache_read_tokens=args.cache_read,
        cache_ttl=args.ttl,
    )
    c = cost(usage, args.model)
    rows = [
        ["input", f"{usage.input_tokens:,}", fmt_usd(c.input)],
        ["output", f"{usage.output_tokens:,}", fmt_usd(c.output)],
        ["cache write", f"{usage.cache_write_tokens:,}", fmt_usd(c.cache_write)],
        ["cache read", f"{usage.cache_read_tokens:,}", fmt_usd(c.cache_read)],
        ["TOTAL", f"{usage.total_tokens:,}", fmt_usd(c.total)],
    ]
    print(f"Cost on {resolve(args.model)}  (x{args.requests:,} requests)\n")
    print(table(["line", "tokens", "cost"], rows, indent="    "))
    if args.requests > 1:
        print(f"\n    {args.requests:,} requests: {fmt_usd(c.total * args.requests)}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    rows = []
    for name in PRICES:
        p = price_of(name)
        rows.append([
            name,
            fmt_usd(p.input),
            fmt_usd(p.cache_write_5m),
            fmt_usd(p.cache_hit),
            fmt_usd(p.output),
            f"{p.cache_min_tokens:,}",
            f"{p.context_window:,}",
        ])
    print("Rates in USD per million tokens\n")
    print(table(
        ["model", "input", "write 5m", "hit", "output", "cache min", "context"],
        rows, indent="    ",
    ))
    print("\n    Effort levels (cheapest first): " + ", ".join(EFFORT_LEVELS))
    print("    Default effort: high. Thinking is on by default on Opus 5.")
    return 0


def cmd_passes(args: argparse.Namespace) -> int:
    rows = [[p.name, "aggressive" if p.aggressive else "safe", p.why] for p in PASSES]
    print("Compression passes\n")
    print(table(["name", "class", "what it does"], rows, indent="    "))
    print("\n    Safe passes run by default; add --aggressive for the rest.")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="opus5-lean",
        description="Get the best Claude Opus 5 output per token spent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  opus5-lean count prompt.md --exact\n"
            "  opus5-lean slim prompt.md -o prompt.lean.md --aggressive\n"
            "  opus5-lean cache segments.json --rpd 5000\n"
            "  opus5-lean sweep task.md --require 'def ,return' --trials 3\n"
        ),
    )
    ap.add_argument("--version", action="version", version=f"opus5-lean {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_model(p: argparse.ArgumentParser) -> None:
        p.add_argument("-m", "--model", default="claude-opus-5", help="model for pricing")

    p = sub.add_parser("count", help="token count + cost for a file")
    p.add_argument("file", help="file to count, or - for stdin")
    add_model(p)
    p.add_argument("--exact", action="store_true", help="use the unbilled count_tokens API")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_count)

    p = sub.add_parser("slim", help="compress a prompt")
    p.add_argument("file")
    add_model(p)
    p.add_argument("-o", "--output", help="write compressed text here")
    p.add_argument("--stdout", action="store_true", help="write result to stdout only")
    p.add_argument("--aggressive", action="store_true", help="also run content-changing passes")
    p.add_argument("--only", help="comma-separated pass names")
    p.add_argument("--exclude", help="comma-separated pass names to skip")
    p.add_argument("--exact", action="store_true")
    p.add_argument("--requests", type=int, default=1000, help="scale savings by this many")
    p.add_argument("--min-saving", type=float, default=0.0,
                   help="exit 1 if savings are below this %% (for CI)")
    p.set_defaults(func=cmd_slim)

    p = sub.add_parser("cache", help="plan cache_control breakpoints")
    p.add_argument("file", help="JSON segment spec")
    add_model(p)
    p.add_argument("--ttl", choices=["5m", "1h"], default="5m")
    p.add_argument("--requests", type=int, default=100)
    p.add_argument("--rpd", type=int, help="requests per day, for a monthly projection")
    p.add_argument("--exact", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--strict", action="store_true", help="exit 1 if there are warnings")
    p.set_defaults(func=cmd_cache)

    p = sub.add_parser("sweep", help="find the cheapest effort level (needs a paid key)")
    p.add_argument("file")
    add_model(p)
    p.add_argument("--system", help="file containing a system prompt")
    p.add_argument("--require", help="comma-separated substrings the output must contain")
    p.add_argument("--forbid", help="comma-separated substrings that fail the output")
    p.add_argument("--threshold", type=float, default=1.0)
    p.add_argument("--levels", help=f"comma-separated subset of {','.join(EFFORT_LEVELS)}")
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--save", help="write results as JSON")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("cost", help="price a token count")
    add_model(p)
    p.add_argument("-i", "--input", type=int, default=0)
    p.add_argument("-o", "--output", type=int, default=0)
    p.add_argument("--cache-write", type=int, default=0)
    p.add_argument("--cache-read", type=int, default=0)
    p.add_argument("--ttl", choices=["5m", "1h"], default="5m")
    p.add_argument("--requests", type=int, default=1)
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("models", help="show the price table")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("passes", help="list compression passes")
    p.set_defaults(func=cmd_passes)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
