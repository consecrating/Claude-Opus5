# opus5-lean

Token-efficiency toolkit for **Claude Opus 5**. Measure what a prompt costs,
strip what it doesn't need, cache what doesn't change, and find the cheapest
effort level that still holds quality.

Opus 5 is $5/MTok in and $25/MTok out, thinking is on by default, and cache
reads cost a tenth of a fresh input token. Most real workloads leave 80%+ on
the table. This finds it.

```
$ opus5-lean cache examples/segments.json --rpd 5000

  36,100/39,620 tokens cacheable (91%), 3 breakpoint(s), break-even after 1 hit(s)

    segment              tokens  cumulative  volatility
    -------------------  ------  ----------  ------------------------
    tool schemas          4,200       4,200  static (never changes)
    system prompt         6,100      10,300  static (never changes)    <== cache_control
    product docs (RAG)   24,000      34,300  slow (changes on deploy)  <== cache_control
    account context       1,800      36,100  per-session               <== cache_control
    conversation so far   3,400      39,500  per-turn
    user turn               120      39,620  per-turn

  At 5,000 requests/day for 30 days (150,000 requests):
    without cache  $29,715.00
    with cache     $5,496.95
    saved          $24,218.05  (82%)
```

## Install

No dependencies. The offline tools work straight from a clone.

```bash
git clone https://github.com/consecrating/Claude-Opus5.git
cd Claude-Opus5
export PYTHONPATH=src
python -m opus5lean models
```

Or install the command:

```bash
pip install -e .
opus5-lean models
```

Python 3.10+.

## Do I need an API key?

Mostly no — and there is a detail worth knowing.

| Command | Key needed | Cost |
|---|---|---|
| `count`, `slim`, `cache`, `cost`, `models`, `passes` | No | Free, offline |
| `count --exact` | Yes | **Free** — `count_tokens` is unbilled |
| `sweep` | Yes, with credit | Real completions |

**`count_tokens` is not billed.** A key with a $0 balance gives you exact token
counts for a full request, tool schemas included. Everything except `sweep`
costs nothing.

There is no free tier for Opus 5 itself, and "free Claude API key" sites are
stolen-credential proxies — don't. See
**[docs/FREE-API-KEYS.md](docs/FREE-API-KEYS.md)** for legitimate free
providers to use for the structural half of the work, and for how to hand a key
to Kiro without pasting it into chat.

## The four tools

### `count` — what does this actually cost?

```bash
opus5-lean count prompt.md            # offline estimate
opus5-lean count prompt.md --exact    # exact, still free
```

```
agent_prompt.md: ~467 tokens  [estimate]  model=claude-opus-5
  1,546 chars (3.31 chars/token), 206 in code blocks
  cost if sent as input:  $0.002335   |  as output: $0.0117
  context used: 0.05% of 1,000,000  ........................
```

Estimates are always marked `~` and labelled. A number you could bill against
is never confusable with one you couldn't.

### `slim` — remove tokens that carry no instruction

```bash
opus5-lean slim prompt.md --aggressive -o prompt.lean.md
```

```
  ~467 -> ~388 tokens   saved 79 (16.9%)

    pass           saved  of total  what it does
    -------------  -----  --------  -----------------------------------
    dedupe            34      7.3%  drop repeated long lines
    html-comments     18      3.9%  drop <!-- --> comments
    spaces             5      1.1%  collapse runs of spaces in a line
    unicode            4      0.9%  typographic chars -> cheaper ASCII
```

**Code is never touched** — fenced blocks and inline spans are lifted out and
restored byte-for-byte, indentation intact. Every pass reports what it actually
saved, so you can drop the ones that don't earn their place. Passes that change
rendered output are opt-in behind `--aggressive`.

Gate it in CI so prompts don't re-bloat:

```bash
opus5-lean slim prompt.md --min-saving 5   # exit 1 if 5% slack crept back
```

### `cache` — the big one

Cache reads cost **10x less** than input tokens, and the 1.25x write premium is
repaid after less than one reuse. Describe your prompt's segments and how often
each changes:

```json
[
  { "name": "tool schemas",  "tokens": 4200,  "volatility": 0 },
  { "name": "system prompt", "tokens": 6100,  "volatility": 0 },
  { "name": "conversation",  "tokens": 3400,  "volatility": 3 }
]
```

`tokens` can be replaced with `file` or `text` and it'll count for you.

It places up to four breakpoints at volatility boundaries, projects savings, and
catches the failure that actually bites people:

```
  ! prefix-cache ordering problem: 'system prompt' appears after more volatile
    content, so it can never be cached. Move stable content to the front.
```

The cache matches on *prefix*. One timestamp near the top invalidates
everything after it, and the symptom is a cache that silently never hits.

### `sweep` — the cheapest effort level that still works

Needs credit; it makes real calls.

```bash
opus5-lean sweep task.md --require "def ,return" --trials 3
```

Report shape (illustrative figures — unlike the other examples on this page,
this one wasn't captured from a run, since it needs a funded key):

```
    effort   score  out tok  cost/req  latency
    ------  ------  -------  --------  -------
    low       0.50      412   $0.0113     3.2s
    medium    1.00    1,190   $0.0312     7.9s  PASS
    high      1.00    3,540   $0.0898    19.4s  PASS

  Recommended: effort=medium
  65% cheaper than the default (high): $0.0312 vs $0.0898 per request
```

Without a grader it reports cost and latency but **makes no recommendation** —
choosing an effort level on cost alone is how quality regressions ship.

Re-sweep when migrating: Opus 5 responds to each level differently than Opus
4.8, so inherited settings aren't valid.

## Library use

```python
from opus5lean import count_tokens, slim, plan_cache, cost, Usage
from opus5lean.cache import Segment

exact = count_tokens(open("prompt.md").read())     # free, unbilled
print(exact.tokens, exact.method)                  # 6100 exact

lean = slim(open("prompt.md").read(), aggressive=True)
print(f"saved {lean.saved} tokens ({lean.pct:.1f}%)")

plan = plan_cache([
    Segment("system", 6100, volatility=0),
    Segment("turn", 400, volatility=3),
])
print(plan.summary(), plan.savings(1000))

print(cost(Usage(input_tokens=6500, output_tokens=1200)).total)
```

## What this gets right about Opus 5

The mistakes below are easy to make and expensive:

- **Thinking is on by default**, so `content[0]` is often a thinking block, not
  text. `AnthropicClient` selects by block `type`. Thinking bills as output and
  counts against `max_tokens`.
- **`thinking: disabled` returns 400 at `xhigh`/`max`.** Refused locally with an
  explanation instead of a wasted round trip.
- **Changing top-level `effort` invalidates the prompt cache.** Documented at
  every place it's relevant.
- **Effort doesn't shorten prose** — it controls thinking volume. Prompt for
  length instead.
- **Opus 5's cacheable minimum is 512 tokens** (1,024 on Opus 4.8), so prompts
  that couldn't be cached before now can. The planner knows the difference.

## Using it with Kiro

`.kiro/steering/token-efficiency.md` ships as workspace steering: it gives Kiro
the pricing facts, the API gotchas above, and the invariants to preserve when
editing this code (estimates stay labelled, code never gets reformatted,
offline commands stay offline).

To make it a full Power with callable tools, wrap the CLI in an MCP server —
the commands are already single-purpose and JSON-capable (`--json`), which is
most of that work.

## Development

```bash
pip install -e '.[dev]'
pytest          # 87 tests, no network, no key
ruff check .
```

Tests are offline by construction. `pricing.py` is the single source for every
rate; nothing else hardcodes a price.

## Docs

- **[docs/TOKEN-PLAYBOOK.md](docs/TOKEN-PLAYBOOK.md)** — every technique,
  ordered by payoff, with the arithmetic.
- **[docs/FREE-API-KEYS.md](docs/FREE-API-KEYS.md)** — what's actually free,
  which providers are legitimate, and how to hand a key to Kiro safely.

## Caveats

- Prices were verified against Anthropic's docs in September 2026 and live in
  `src/opus5lean/pricing.py`. Re-check before trusting a projection.
- The offline estimator is a heuristic, typically within ~10-15%. Use `--exact`
  for anything you'll act on; it's free.
- `cache` projections assume your volatility labels are accurate. Garbage in,
  confident-looking garbage out.
- Live API paths (`sweep`, `count --exact`) are built to the documented request
  shapes but are not exercised by the test suite, which runs without a key.

## License

MIT
