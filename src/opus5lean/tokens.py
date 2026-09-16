"""Token measurement.

Two paths, and the distinction matters:

**Exact** (:func:`count_tokens`) calls Anthropic's ``/v1/messages/count_tokens``
endpoint. That endpoint is *not billed* -- it is the one part of the API you can
hammer at zero cost. If you have a key with no credit on it, you can still do
all of your token accounting exactly. Use this for anything you're going to
make a decision on.

**Estimated** (:func:`estimate_tokens`) needs no key and no network. It
segments text into code and prose and applies separate ratios, which is
noticeably better than the usual ``len(text) / 4``, but it is still a heuristic
and typically lands within roughly 10-15% of the real count. Use it for fast
iteration, relative comparisons ("did this edit help?"), and CI checks where a
network call would be inappropriate -- then confirm with an exact count.

Every number this module returns carries its ``method``, so a report can never
silently present an estimate as ground truth.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .pricing import resolve

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_VERSION = "2023-06-01"


class TokenCountError(RuntimeError):
    """Raised when an exact count could not be obtained."""


@dataclass(frozen=True)
class CountResult:
    """A token count plus how it was arrived at.

    ``method`` is ``"exact"`` for an API-derived count and ``"estimate"`` for a
    local heuristic. Never drop this field when passing counts around.
    """

    tokens: int
    method: str
    model: str = "claude-opus-5"
    detail: dict[str, int] = field(default_factory=dict)

    @property
    def is_exact(self) -> bool:
        return self.method == "exact"

    def __str__(self) -> str:
        mark = "" if self.is_exact else "~"
        return f"{mark}{self.tokens:,} tokens ({self.method})"


# --------------------------------------------------------------------------
# Offline estimation
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)

# One alternative per token-ish class, so a single pass classifies everything.
_PIECE_RE = re.compile(
    r"""
      (?P<word>[A-Za-z]+(?:'[A-Za-z]+)?)   # words, contractions kept whole
    | (?P<num>\d+)                          # digit runs
    | (?P<cjk>[\u3000-\u9fff\uac00-\ud7af]) # CJK/Hangul: ~1 token per char
    | (?P<space>[ \t]+)                     # horizontal whitespace
    | (?P<newline>\n)                       # newlines
    | (?P<punct>[^\sA-Za-z\d]+)             # punctuation, as runs
    """,
    re.VERBOSE,
)

# BPE merges common short words into one token; longer words split every ~4
# chars. Calibrated so ordinary English prose lands near 3.9 chars/token.
_CHARS_PER_SUBWORD = 4.2


def _estimate_word(length: int) -> int:
    if length <= 5:
        return 1
    return max(1, round(length / _CHARS_PER_SUBWORD))


def _estimate_segment(text: str, code: bool) -> int:
    """Estimate tokens for one homogeneous segment.

    Code is denser than prose: identifiers, punctuation and indentation all
    tokenise less efficiently, so the same character count costs more.
    """
    total = 0
    for m in _PIECE_RE.finditer(text):
        kind = m.lastgroup
        piece = m.group()
        if kind == "word":
            total += _estimate_word(len(piece))
        elif kind == "num":
            # Digits group in runs of up to 3.
            total += max(1, -(-len(piece) // 3))
        elif kind == "cjk":
            total += 1
        elif kind == "space":
            # A single space merges into the neighbouring token; runs of
            # indentation do cost something.
            total += 0 if len(piece) == 1 else max(1, len(piece) // 4)
        elif kind == "newline":
            total += 1
        else:  # punct run
            # BPE merges punctuation runs: "...", "--", "=>" and '{"' are
            # each typically a single token, not one per character.
            total += max(1, round(len(piece) / 2.5))
    if code:
        # Punctuation-dense code fragments tokenise ~15% worse than this
        # piecewise model predicts.
        total = round(total * 1.15)
    return total


def estimate_tokens(text: str, model: str = "claude-opus-5") -> CountResult:
    """Estimate tokens offline, treating fenced code blocks separately.

    No key, no network. Accurate enough to compare two versions of a prompt;
    not accurate enough to bill against.
    """
    if not text:
        return CountResult(tokens=0, method="estimate", model=model)

    prose_chars = 0
    code_chars = 0
    total = 0
    cursor = 0
    for m in _FENCE_RE.finditer(text):
        prose = text[cursor : m.start()]
        total += _estimate_segment(prose, code=False)
        prose_chars += len(prose)
        total += _estimate_segment(m.group(), code=True)
        code_chars += len(m.group())
        cursor = m.end()
    tail = text[cursor:]
    total += _estimate_segment(tail, code=False)
    prose_chars += len(tail)

    # Every request carries a little structural overhead beyond the text.
    total += 3

    return CountResult(
        tokens=total,
        method="estimate",
        model=model,
        detail={
            "chars": len(text),
            "prose_chars": prose_chars,
            "code_chars": code_chars,
        },
    )


# --------------------------------------------------------------------------
# Exact counting
# --------------------------------------------------------------------------


def count_tokens(
    text: str | None = None,
    *,
    model: str = "claude-opus-5",
    system: str | None = None,
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
    api_key: str | None = None,
    timeout: float = 30.0,
    fallback: bool = False,
) -> CountResult:
    """Count tokens exactly via Anthropic's unbilled ``count_tokens`` endpoint.

    Pass either ``text`` (wrapped as a single user message) or a full
    ``messages`` list, optionally with ``system`` and ``tools`` -- tool
    schemas are part of your input and are easy to forget when budgeting.

    Args:
        fallback: If True, fall back to :func:`estimate_tokens` instead of
            raising when no key is set or the request fails. The returned
            ``method`` will be ``"estimate"``, so callers can still tell.

    Raises:
        TokenCountError: On any failure, unless ``fallback`` is set.
    """
    if messages is None:
        if text is None:
            raise ValueError("pass either text= or messages=")
        messages = [{"role": "user", "content": text}]

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        if fallback:
            return estimate_tokens(_flatten(messages, system, tools), model=model)
        raise TokenCountError(
            "ANTHROPIC_API_KEY is not set. The count_tokens endpoint is not "
            "billed, so a key with zero credit is enough. Or pass "
            "fallback=True to use the offline estimator."
        )

    payload: dict = {"model": resolve_api_model(model), "messages": messages}
    if system is not None:
        payload["system"] = system
    if tools:
        payload["tools"] = tools

    req = urllib.request.Request(
        ANTHROPIC_ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # pragma: no cover - network
        detail = exc.read().decode(errors="replace")[:400]
        if fallback:
            return estimate_tokens(_flatten(messages, system, tools), model=model)
        raise TokenCountError(f"count_tokens failed: HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # pragma: no cover - network
        if fallback:
            return estimate_tokens(_flatten(messages, system, tools), model=model)
        raise TokenCountError(f"count_tokens failed: {exc}") from exc

    if "input_tokens" not in body:
        if fallback:
            return estimate_tokens(_flatten(messages, system, tools), model=model)
        raise TokenCountError(f"unexpected response: {body}")

    return CountResult(tokens=int(body["input_tokens"]), method="exact", model=model)


def resolve_api_model(model: str) -> str:
    """Map an internal price-table key to the string the API expects."""
    canonical = resolve(model)
    api_names = {
        "claude-opus-5": "claude-opus-5",
        "claude-opus-5-fast": "claude-opus-5",
        "claude-opus-4-8": "claude-opus-4-8",
        "claude-sonnet-5": "claude-sonnet-5",
        "claude-haiku-4-5": "claude-haiku-4-5-20251001",
        "claude-fable-5": "claude-fable-5",
        "claude-fable-5-1": "claude-fable-5-1",
    }
    return api_names.get(canonical, canonical)


def _flatten(messages: list[dict], system: str | None, tools: list[dict] | None) -> str:
    """Concatenate a request's text for offline estimation."""
    parts: list[str] = []
    if system:
        parts.append(system)
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    if tools:
        parts.append(json.dumps(tools))
    return "\n".join(parts)


def count(text: str, *, model: str = "claude-opus-5", exact: bool = False) -> CountResult:
    """Count tokens, preferring exactness when asked but never failing hard."""
    if exact:
        return count_tokens(text, model=model, fallback=True)
    return estimate_tokens(text, model=model)
