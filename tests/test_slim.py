"""Compression passes.

The load-bearing test in this file is `test_code_blocks_are_untouched`. A
compressor that silently reformats Python indentation or YAML is worse than no
compressor, because the damage is invisible until runtime.
"""

from __future__ import annotations

import itertools

import pytest

from opus5lean.slim import (
    PASSES,
    collapse_blank_lines,
    collapse_spaces,
    dedupe_lines,
    normalize_unicode,
    slim,
    strip_emphasis,
    strip_html_comments,
    strip_trailing_whitespace,
)

CODE_PROMPT = """\
# Guide

Some     text   with    slack.


```python
def f(x):
    if x:
        return    "keep     this  exactly"
    return None
```

More      text.
"""


def test_code_blocks_are_untouched():
    result = slim(CODE_PROMPT, aggressive=True)
    assert 'return    "keep     this  exactly"' in result.slimmed
    assert "    if x:" in result.slimmed  # indentation preserved


def test_prose_outside_code_is_compressed():
    result = slim(CODE_PROMPT)
    assert "Some text with slack." in result.slimmed
    assert "More text." in result.slimmed


def test_inline_code_is_untouched():
    text = "Call `foo(  a,   b )` now."
    assert "`foo(  a,   b )`" in slim(text, aggressive=True).slimmed


def test_savings_are_reported_and_positive():
    result = slim(CODE_PROMPT)
    assert result.saved > 0
    assert result.pct > 0
    assert result.before.tokens - result.after.tokens == result.saved


def test_per_pass_attribution_sums_consistently():
    result = slim(CODE_PROMPT, aggressive=True)
    # Each pass records the delta it caused; the chain must be contiguous.
    for a, b in itertools.pairwise(result.passes):
        assert a.tokens_after == b.tokens_before


def test_effective_passes_excludes_no_ops_and_sorts_by_win():
    result = slim(CODE_PROMPT, aggressive=True)
    eff = result.effective_passes
    assert all(p.saved > 0 for p in eff)
    assert eff == sorted(eff, key=lambda p: -p.saved)


def test_slim_is_idempotent():
    once = slim(CODE_PROMPT, aggressive=True).slimmed
    twice = slim(once, aggressive=True).slimmed
    assert once == twice


def test_aggressive_saves_at_least_as_much_as_safe():
    safe = slim(CODE_PROMPT).saved
    aggressive = slim(CODE_PROMPT, aggressive=True).saved
    assert aggressive >= safe


def test_safe_mode_keeps_emphasis():
    text = "This is **important** and *notable* prose that must stay readable."
    assert "**important**" in slim(text).slimmed
    assert "**important**" not in slim(text, aggressive=True).slimmed


def test_empty_input():
    result = slim("")
    assert result.slimmed == ""
    assert result.saved == 0
    assert result.pct == 0.0


def test_only_and_exclude_select_passes():
    text = "a  b\n\n\n\nc"
    assert slim(text, only=["spaces"]).slimmed == "a b\n\n\n\nc"
    out = slim(text, exclude=["blank-lines", "trim"]).slimmed
    assert "\n\n\n" in out


def test_unknown_pass_name_is_rejected():
    with pytest.raises(ValueError, match="unknown pass"):
        slim("x", only=["nope"])


def test_pass_registry_names_are_unique():
    names = [p.name for p in PASSES]
    assert len(names) == len(set(names))


# -- individual passes -----------------------------------------------------


def test_normalize_unicode_maps_typography_to_ascii():
    assert normalize_unicode("\u201chi\u201d") == '"hi"'
    assert normalize_unicode("don\u2019t") == "don't"
    assert normalize_unicode("a\u2014b") == "a--b"
    assert normalize_unicode("x\u2026") == "x..."
    assert normalize_unicode("a\u00a0b") == "a b"
    assert normalize_unicode("a\u200bb") == "ab"
    assert normalize_unicode("a \u2192 b") == "a -> b"


def test_strip_trailing_whitespace():
    assert strip_trailing_whitespace("a   \nb\t\n") == "a\nb\n"


def test_collapse_blank_lines_leaves_one_gap():
    assert collapse_blank_lines("a\n\n\n\n\nb") == "a\n\nb"
    assert collapse_blank_lines("a\n\nb") == "a\n\nb"


def test_collapse_spaces_preserves_indentation():
    assert collapse_spaces("    a    b") == "    a b"


def test_strip_html_comments():
    assert strip_html_comments("a<!-- note -->b") == "ab"
    assert strip_html_comments("a<!--\nmulti\n-->b") == "ab"


def test_dedupe_only_removes_long_repeats():
    long = "This sentence is definitely longer than the forty character threshold."
    assert dedupe_lines(f"{long}\n{long}") == long
    assert dedupe_lines("- item\n- item") == "- item\n- item"  # short, kept


def test_dedupe_removes_a_repeated_rule_bullet():
    """A duplicated instruction is waste even when it's formatted as a bullet."""
    rule = "- Never invent an account ID, invoice number, or price."
    assert dedupe_lines(f"{rule}\nsomething else\n{rule}").count("Never invent") == 1


def test_dedupe_matches_a_bullet_against_the_same_rule_as_prose():
    rule = "Escalate to a human when the refund requested is above five hundred."
    assert dedupe_lines(f"- {rule}\n{rule}").count("Escalate") == 1


def test_dedupe_keeps_repeated_headings_and_table_rows():
    assert dedupe_lines("## A section heading that is quite long indeed\n"
                        "## A section heading that is quite long indeed").count("##") == 2
    row = "| alpha | beta | gamma | delta | epsilon | zeta | eta |"
    assert dedupe_lines(f"{row}\n{row}").count("alpha") == 2


def test_strip_emphasis_leaves_bare_asterisks_alone():
    assert strip_emphasis("**bold**") == "bold"
    assert strip_emphasis("*it*") == "it"
    assert strip_emphasis("2 * 3 * 4") == "2 * 3 * 4"
