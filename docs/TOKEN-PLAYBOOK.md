# Token playbook for Claude Opus 5

Techniques ordered by payoff per hour of work. The numbers come from Anthropic's
published rates for Opus 5 ($5/MTok in, $25/MTok out) — see [Sources](#sources).

Facts this playbook is built on, verified 2026-09:

| | |
|---|---|
| Input / output | $5.00 / $25.00 per MTok |
| Cache write (5m / 1h) | $6.25 / $10.00 per MTok — 1.25x / 2x input |
| Cache read | $0.50 per MTok — **10x cheaper than input** |
| Minimum cacheable prefix | **512 tokens** (was 1,024 on Opus 4.8) |
| Context window | 1M tokens, default and maximum |
| Max output | 128k, **thinking included** |
| Effort ladder | `low` `medium` `high` `xhigh` `max`, default `high` |
| Thinking | **on by default**; billed as output |

---

## 1. Cache the stable prefix — 10x on most of your input

The single largest lever. A cache read costs a tenth of a fresh input token,
and the 1.25x write premium is repaid after **less than one** reuse.

```
              write premium        0.25x of input
break-even =  ────────────── =  ───────────────── = 0.28 hits
              saving per hit      0.9x of input
```

So one reuse already wins on the 5-minute TTL. Two on the 1-hour TTL.

```bash
opus5-lean cache examples/segments.json --rpd 5000
```

On the example workload (36k stable tokens, 3.5k per-turn, 5k requests/day)
this is the difference between **$29,715 and $5,497 per month**.

### The failure mode that matters

The cache matches on **prefix**. One volatile block early in the prompt
invalidates everything after it, and the symptom is silence: a cache that
simply never hits. A timestamp, a session ID, or a "current date" line at the
top of a system prompt will do it.

Order segments **most stable first**:

```
tool schemas -> system prompt -> retrieved docs -> session context -> conversation
   static          static          per-deploy        per-session        per-turn
```

`plan_cache` flags out-of-order segments explicitly. Check for that warning
before you go looking for anything subtler.

### Practical notes

- Four `cache_control` breakpoints maximum per request. Spend them at
  volatility boundaries, not evenly.
- Opus 5's 512-token minimum means prompts that couldn't be cached on Opus 4.8
  now can, with no code change.
- **Changing top-level `effort` between requests invalidates the cache.** Pick
  a level per workload and hold it. To vary within a conversation, use the
  per-message `output_config` form, which preserves the prefix.

## 2. Sweep effort — often 40-60% off with no quality loss

Opus 5 converts effort into quality more reliably than earlier models, which
cuts both ways: the level you pick matters more, and `low`/`medium` are
genuinely usable. Anthropic's own guidance is to treat them as the primary cost
control and step down wherever evals hold.

**Re-sweep when you migrate.** Effort settings carried over from Opus 4.8 are
not valid here — the model's response to each level changed.

```bash
opus5-lean sweep task.md --require "def ,return" --trials 3
```

The output names the cheapest level that passed and what it saves against the
`high` default. Without a grader (`--require`), the tool reports cost and
latency but **refuses to recommend a level** — picking on cost alone is how
quality regressions ship.

Rough guide, but measure rather than trust it:

| Level | Use for |
|---|---|
| `low` | Subagents, classification, extraction, formatting |
| `medium` | Routine agentic work — the usual step-down |
| `high` | Default. Complex reasoning, difficult code |
| `xhigh` | Long-horizon agentic work, 30+ minutes |
| `max` | Frontier problems only; often overthinks structured output |

Two constraints specific to Opus 5:

- `thinking: {"type": "disabled"}` returns **400** at `xhigh` or `max`. Drop to
  `high` or keep thinking on.
- At `xhigh`/`max`, set a large `max_tokens` (64k is a sane start). It caps
  thinking *and* text together, so a tight limit truncates the answer.

**Effort does not shorten prose on Opus 5.** It controls thinking volume. If
responses are too long, say so in the prompt — lowering effort won't do it.

## 3. Compress the prompt — 10-25% for free

A long system prompt is usually 10-25% formatting slack rather than
instruction.

```bash
opus5-lean slim prompt.md --aggressive -o prompt.lean.md
```

Ranked by what actually pays on real prompts:

1. **Duplicated rules** (`dedupe`) — the same instruction restated across
   sections. Biggest single win on prompts assembled from templates.
2. **HTML comments** (`html-comments`) — the model reads them; you don't.
3. **Whitespace** (`spaces`, `blank-lines`, `trailing-ws`, `headings`).
4. **Typography** (`unicode`) — a curly apostrophe breaks word merging, so
   `don’t` costs ~3 tokens where `don't` costs 1. Worth the most on text that
   came out of a word processor.
5. **Emphasis** (`emphasis`) — each `**` pair is styling you pay for.

Code blocks and inline spans are never touched. Whitespace is semantic in
Python, YAML and Markdown; a compressor that reformats it is a bug.

Gate it in CI so prompts don't re-bloat:

```bash
opus5-lean slim prompt.md --min-saving 5   # exit 1 if >5% slack has crept back
```

## 4. Measure exactly, and for free

`count_tokens` is **not billed**. A key with zero balance gives you exact
counts for the whole request — including tool schemas, which are easy to forget
and are frequently the largest single block.

```bash
opus5-lean count prompt.md --exact
```

Never budget against an estimate when an exact count is free. The offline
estimator here is good enough to compare two revisions (`~10-15%`), and it
labels every number it produces so an estimate can't be mistaken for a
measurement.

## 5. Route by difficulty

Not every call needs Opus 5. Haiku 4.5 is **1/5 the input and output price**.
An Opus-5-quality answer to "is this ticket about billing?" is waste.

A classifier on Haiku that escalates the hard 20% to Opus 5 typically beats
all-Opus 5 by 3-4x, and `low` effort on Opus 5 is often the better move for
mid-difficulty work.

## 6. Watch output tokens — they cost 5x input

Output is $25/MTok against $5 in, and since **thinking is billed as output and
is on by default**, a workload migrated from Opus 4.8 without thinking will
show more output tokens at the same rates. Re-baseline after migrating.

- Ask for the format you want. "Reply with a JSON object, no prose" is cheaper
  than a paragraph plus JSON.
- Remove verification instructions carried over from older models. Opus 5
  verifies its own work; "include a final verification step" causes
  *over*-verification and burns tokens.
- Cap `max_tokens` to something realistic — but remember it bounds thinking
  too.

---

## Priority order

| Technique | Typical saving | Effort |
|---|---|---|
| Cache the stable prefix | up to 80-90% of input | Low |
| Sweep effort | 40-60% | Low |
| Route easy calls to Haiku | 60-80% on that slice | Medium |
| Compress the prompt | 10-25% of input | Trivial |
| Constrain output format | 20-40% of output | Low |

Caching and effort first. They are the cheapest to implement and by far the
largest.

## Sources

- [What's new in Claude Opus 5](https://platform.claude.com/docs/en/about-claude/models/whats-new-opus-5)
- [Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

Rates and limits change. Re-verify against those pages and update
`src/opus5lean/pricing.py`, which is the single place this repo stores them.

Content was rephrased for compliance with licensing restrictions.
