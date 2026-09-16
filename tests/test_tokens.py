"""Offline token estimation.

These assert the estimator stays in a defensible range rather than hitting
exact numbers -- it is a heuristic, and pinning it to exact values would make
the tests break on every harmless tuning change.
"""

from __future__ import annotations

import pytest

from opus5lean.tokens import (
    CountResult,
    TokenCountError,
    count,
    count_tokens,
    estimate_tokens,
    resolve_api_model,
)


def test_empty_text_is_zero():
    assert estimate_tokens("").tokens == 0


def test_result_carries_its_method():
    r = estimate_tokens("hello world")
    assert r.method == "estimate"
    assert not r.is_exact
    assert str(r).startswith("~")


def test_english_prose_lands_near_four_chars_per_token():
    prose = (
        "The quick brown fox jumps over the lazy dog. Prompt engineering is the "
        "practice of structuring text so that a language model produces a useful "
        "result. Careful writing reduces both cost and latency in production."
    ) * 4
    ratio = len(prose) / estimate_tokens(prose).tokens
    assert 3.2 <= ratio <= 5.0, f"chars/token = {ratio:.2f}"


def test_longer_text_costs_more():
    short = estimate_tokens("hello world").tokens
    long = estimate_tokens("hello world " * 50).tokens
    assert long > short * 10


def test_code_is_priced_denser_than_prose():
    """Same character budget, but punctuation-heavy code should cost more."""
    code = "```python\n" + "x = foo(bar[i], baz={'k': 1})\n" * 12 + "```"
    prose = "a" * len(code)
    assert estimate_tokens(code).tokens > estimate_tokens(prose).tokens


def test_detail_splits_code_from_prose():
    text = "Intro paragraph.\n\n```js\nconst a = 1;\n```\n\nOutro."
    detail = estimate_tokens(text).detail
    assert detail["code_chars"] > 0
    assert detail["prose_chars"] > 0
    assert detail["prose_chars"] + detail["code_chars"] == detail["chars"]


def test_cjk_counted_per_character():
    assert estimate_tokens("日本語のテキスト").tokens >= 8


def test_curly_quotes_cost_more_than_ascii():
    """The premise behind the `unicode` compression pass."""
    fancy = estimate_tokens("\u201cdon\u2019t\u201d \u2014 she said\u2026").tokens
    plain = estimate_tokens('"don\'t" -- she said...').tokens
    assert fancy >= plain


def test_count_without_exact_never_touches_network():
    r = count("some text", exact=False)
    assert r.method == "estimate"


def test_count_tokens_requires_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(TokenCountError, match="not billed"):
        count_tokens("hi")


def test_count_tokens_falls_back_and_says_so(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = count_tokens("hi there", fallback=True)
    assert isinstance(r, CountResult)
    assert r.method == "estimate"  # caller can still tell it wasn't exact


def test_count_tokens_rejects_empty_call():
    with pytest.raises(ValueError, match="text=|messages="):
        count_tokens()


def test_api_model_names():
    assert resolve_api_model("opus-5") == "claude-opus-5"
    assert resolve_api_model("claude-opus-5-fast") == "claude-opus-5"
    assert resolve_api_model("haiku-4.5") == "claude-haiku-4-5-20251001"
