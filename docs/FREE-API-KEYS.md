# Getting an API key (and what "free" actually buys you)

Read the first section before anything else. It will save you from a
disappointment and from a bad decision.

## 1. There is no free Claude Opus 5

Anthropic does not operate a permanent free tier. Opus 5 is $5/MTok in and
$25/MTok out, and no legitimate route around that exists. New accounts have at
times carried small trial credits, but that is not something to build on.

**So do not go looking for a "free Claude API key" site.** They exist in
numbers, and they are one of three things:

- a reverse proxy in front of somebody else's stolen key,
- a credential harvester that keeps whatever key you paste into it,
- a service that reads every prompt you send through it.

Using the first is theft of service and gets the underlying account banned.
The second and third are worse for you specifically. This repository will not
point at any of them, and the toolkit deliberately reads keys from the
environment so you never paste one into a web form to "test" it.

The good news is that most of what this toolkit does needs no paid key at all.

## 2. What is genuinely free on the Anthropic API

**The `count_tokens` endpoint is not billed.** This is the important one. It
returns exact token counts for a full request — system prompt, messages, tool
schemas and all — and charges nothing.

Practically: create an Anthropic account, generate a key, put **zero credit on
it**, and you can still run all of the exact measurement in this toolkit.

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # a key with $0 balance is fine
opus5-lean count examples/agent_prompt.md --exact
```

Get a key at [console.anthropic.com](https://console.anthropic.com/settings/keys).

Only `sweep` needs real credit, because it makes real completions.

## 3. Genuinely free keys, for the parts that don't need Claude

These are real free tiers from real providers. Use them for the structural half
of the work — does the prompt parse, does the tool loop terminate, does the
output validate, does the retry path fire — then spend Opus 5 tokens only on
the runs that need Opus 5 quality.

All of the following speak the OpenAI `/chat/completions` shape, so
`OpenAICompatClient` talks to any of them with a base-URL change.

| Provider | Card needed? | Base URL | Notes |
|---|---|---|---|
| [Groq](https://console.groq.com/keys) | No | `https://api.groq.com/openai/v1` | Fastest to get going; very high daily request allowance on small models |
| [Google AI Studio](https://aistudio.google.com/apikey) | No | `https://generativelanguage.googleapis.com/v1beta/openai` | Gemini models; large free daily quota |
| [OpenRouter](https://openrouter.ai/keys) | No | `https://openrouter.ai/api/v1` | Models with a `:free` suffix; low daily cap until you add credit |
| [Cerebras](https://cloud.cerebras.ai/) | No | `https://api.cerebras.ai/v1` | Very high tokens/sec |
| [Mistral](https://console.mistral.ai/) | No | `https://api.mistral.ai/v1` | Free experimentation tier |
| [GitHub Models](https://github.com/marketplace/models) | No | `https://models.inference.ai.azure.com` | Uses your GitHub account |
| [Cloudflare Workers AI](https://dash.cloudflare.com/) | No | `https://api.cloudflare.com/client/v4/accounts/<id>/ai/v1` | Free daily neuron allowance |

**Rate limits and model lists on free tiers change frequently.** The numbers
that circulate in blog posts go stale fast, so check the provider's own pricing
page rather than trusting a figure quoted here or elsewhere. If you only want
one: **Groq** is the least friction — no card, and a key in about a minute.

### Configure one

```bash
cp .env.example .env
```

```bash
OPENAI_COMPAT_BASE_URL=https://api.groq.com/openai/v1
OPENAI_COMPAT_API_KEY=gsk_...
OPENAI_COMPAT_MODEL=llama-3.3-70b-versatile
```

## 4. Providing the key in Kiro

Kiro runs your commands in the workspace, so it picks up whatever the shell
has. Two options, in order of preference:

**Use a `.env` file.** `.gitignore` already excludes it. Ask Kiro to run
commands with it loaded:

```bash
set -a && . ./.env && set +a && opus5-lean count prompt.md --exact
```

**Or export for the session:**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Two things to keep in mind when working with an agent:

- **Don't paste a key into chat.** Chat transcripts get stored and shared. Put
  it in `.env` and tell the agent the file exists — it never needs to see the
  value to use it.
- **Rotate anything that leaks.** If a key does end up in a transcript, a
  commit, or a log, revoke it in the console rather than hoping. Keys are free
  to replace.

## 5. The order that costs least

1. **Measure offline.** `count`, `slim` and `cache` need no key at all.
2. **Confirm exactly.** `count --exact` with a $0-balance key.
3. **Debug structure on a free provider.** Prompt shape, tool loops, parsing.
4. **Then buy Opus 5 tokens** for quality-sensitive runs — with a cache plan
   already in place and a swept effort level, so you're not paying 10x on a
   prefix you could have cached or spending `max` effort where `medium` held.

Steps 1–3 are where most of the waste gets removed, and they cost nothing.

---

Provider details were gathered from each provider's own documentation, plus
[OpenRouter's free-model comparison](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/)
and [Dataiku's provider round-up](https://www.dataiku.com/blog/best-llm-apis-for-developers).
Pricing and endpoint facts for Claude come from
[Anthropic's platform docs](https://platform.claude.com/docs/en/about-claude/models/whats-new-opus-5).
Content was rephrased for compliance with licensing restrictions.
