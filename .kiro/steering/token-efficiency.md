---
inclusion: always
---

# Token efficiency

Rules for working on this repo, and for any prompt or API call built here.

## Before changing a price or limit

`src/opus5lean/pricing.py` is the only place rates live. Don't hardcode a price
anywhere else. When updating it, cite the Anthropic docs page you took the
number from and update the "verified" date in `docs/TOKEN-PLAYBOOK.md`.

## Never present an estimate as a measurement

`CountResult.method` is `"exact"` or `"estimate"` and must survive every hop.
Estimates render with a `~` prefix. `count_tokens` is unbilled, so there is no
excuse for guessing when a decision depends on the number.

## Compression must not touch code

Fenced blocks and inline spans are lifted out before any pass and restored
byte-for-byte. Whitespace is semantic in Python, YAML and Markdown. A new pass
that could reformat code is a bug, not a feature — and it needs a test proving
it doesn't.

Every pass reports its own token delta. A pass that can't show what it saved
doesn't belong in the registry. Passes that change rendered output are
`aggressive=True` and stay off by default.

## Opus 5 API facts to get right

These are easy to get wrong and expensive when you do:

- **Thinking is on by default.** Select response content by block `type`, never
  by index — `content[0]` is frequently a thinking block. Thinking bills as
  output and counts against `max_tokens`.
- **`thinking: disabled` is a 400 at `xhigh`/`max`.** Refuse it locally with a
  clear message rather than letting the API reject it.
- **Changing top-level `effort` between requests invalidates the prompt cache.**
  Hold it constant within a cached conversation; use per-message
  `output_config` to vary it.
- **Effort doesn't shorten prose** on Opus 5 — it controls thinking volume.
  Prompt for length instead.
- **Don't add verification instructions.** Opus 5 verifies its own work;
  telling it to causes over-verification and burns tokens.
- Cacheable minimum is **512** tokens on Opus 5, 1,024 on Opus 4.8.
- Four `cache_control` breakpoints maximum.

## When adding a feature

Offline first. `count`, `slim`, `cache` and `cost` must keep working with no
key and no network — that constraint is why the core is stdlib-only. Don't add
a runtime dependency to the core for convenience; put it under the `live` extra.

Anything that spends money says so in its `--help` and fails with an
explanation, not a traceback, when credentials are missing.

## Secrets

Keys come from the environment. Never write one into a file, a test, a fixture
or a commit. `.env` is gitignored; keep it that way.

## Ordering advice we give users

When advising on cost, lead with caching and effort — they're the two largest
levers by an order of magnitude. Compression is real but smaller; don't present
it as the headline.
