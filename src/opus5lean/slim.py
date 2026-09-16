"""Prompt compression: remove tokens that carry no instruction.

The premise is that a large fraction of a long system prompt is not
instruction at all -- it is formatting slack. Trailing whitespace, triple blank
lines, curly quotes that cost three tokens where an ASCII quote costs one,
HTML comments the model reads and you don't. Removing that is free: the text
means exactly the same thing afterwards.

Two rules keep this trustworthy:

1. **Code is never touched.** Fenced blocks and inline spans are lifted out
   before any pass runs and put back afterwards, byte for byte. Whitespace is
   semantic in Python, YAML and Markdown code; a compressor that reformats it
   is a compressor that breaks things.
2. **Every pass reports what it actually saved.** Passes are measured
   individually, so you can see which ones earn their place and drop the rest.
   A pass that saves nothing on your prompt is noise.

Passes are split into ``safe`` (byte-level slack; meaning provably unchanged)
and ``aggressive`` (touches rendered content -- real savings, but read the diff
before shipping). Only safe passes run by default.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from .tokens import CountResult, count

# --------------------------------------------------------------------------
# Code protection
# --------------------------------------------------------------------------

_SENTINEL = "\x00\x01"
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _protect(text: str) -> tuple[str, list[str]]:
    """Replace code regions with sentinels so passes cannot alter them."""
    stash: list[str] = []

    def take(m: re.Match[str]) -> str:
        stash.append(m.group())
        return f"{_SENTINEL}{len(stash) - 1}{_SENTINEL}"

    text = _FENCE_RE.sub(take, text)
    text = _INLINE_CODE_RE.sub(take, text)
    return text, stash


def _restore(text: str, stash: list[str]) -> str:
    """Put protected code regions back, innermost markers first."""
    for i in range(len(stash) - 1, -1, -1):
        text = text.replace(f"{_SENTINEL}{i}{_SENTINEL}", stash[i])
    return text


# --------------------------------------------------------------------------
# Safe passes -- byte-level slack only
# --------------------------------------------------------------------------

# Characters that cost more tokens than their ASCII equivalent while reading
# identically to the model. Curly quotes are the big one in prose written in
# a word processor; NBSP is the big one in text pasted from the web.
_UNICODE_SWAPS = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2013": "-", "\u2014": "--", "\u2015": "--", "\u2212": "-",
    "\u2026": "...",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u2009": " ",
    "\u200a": " ", "\u2002": " ", "\u2003": " ", "\ufeff": "",
    "\u200b": "", "\u200c": "", "\u200d": "",
    "\u2022": "-", "\u00b7": "-",
    "\u2192": "->", "\u2190": "<-", "\u21d2": "=>",
    "\u2264": "<=", "\u2265": ">=", "\u2260": "!=",
    "\u00d7": "x", "\u2032": "'", "\u2033": '"',
}
_UNICODE_RE = re.compile("|".join(re.escape(k) for k in _UNICODE_SWAPS))


def normalize_unicode(text: str) -> str:
    """Swap typographic characters for ASCII that tokenises cheaper.

    A curly apostrophe is frequently its own token (sometimes more, as it is
    multi-byte); ``'`` merges into the surrounding word. On prose that came out
    of a word processor this is often the single highest-yield pass.
    """
    text = unicodedata.normalize("NFKC", text)
    return _UNICODE_RE.sub(lambda m: _UNICODE_SWAPS[m.group()], text)


def strip_trailing_whitespace(text: str) -> str:
    """Drop trailing spaces and tabs from every line."""
    return "\n".join(line.rstrip() for line in text.split("\n"))


def collapse_blank_lines(text: str) -> str:
    """Reduce runs of 3+ newlines to a single blank line."""
    return re.sub(r"\n{3,}", "\n\n", text)


def collapse_spaces(text: str) -> str:
    """Collapse runs of spaces *within* a line, preserving indentation."""
    out = []
    for line in text.split("\n"):
        stripped = line.lstrip(" \t")
        indent = line[: len(line) - len(stripped)]
        out.append(indent + re.sub(r"[ \t]{2,}", " ", stripped))
    return "\n".join(out)


def strip_html_comments(text: str) -> str:
    """Remove ``<!-- ... -->`` comments, which the model pays for and reads."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def tighten_xml(text: str) -> str:
    """Remove blank lines between adjacent XML-ish tags.

    Structural prompt tags (``<instructions>``, ``<context>``) are usually
    generated with generous spacing that carries no information.
    """
    return re.sub(r">\n\s*\n+(\s*<)", r">\n\1", text)


def collapse_list_gaps(text: str) -> str:
    """Remove blank lines between consecutive bullet or numbered items."""
    return re.sub(r"(\n\s*(?:[-*+]|\d+\.)\s[^\n]*)\n\n(?=\s*(?:[-*+]|\d+\.)\s)", r"\1\n", text)


def trim_document(text: str) -> str:
    """Strip leading/trailing whitespace from the whole document."""
    return text.strip() + "\n" if text.strip() else ""


# --------------------------------------------------------------------------
# Aggressive passes -- change rendered content
# --------------------------------------------------------------------------


def dedupe_lines(text: str) -> str:
    """Remove exact repeats of substantial lines, keeping the first.

    Prompts assembled from templates accumulate the same rule in several
    sections, and a duplicated instruction is pure cost -- the model already
    had it. A repeated bullet counts: if the same 60-character rule appears in
    both "Rules" and "Escalation policy", the second one is waste.

    Two exclusions, because repetition there is structural rather than
    redundant: headings (distinct sections legitimately share a title) and
    table rows (identical rows can be meaningful data).
    """
    min_len = 40
    seen: set[str] = set()
    out: list[str] = []
    for line in text.split("\n"):
        key = line.strip()
        # Compare bullets by their content, so "- x" and "x" dedupe together.
        body = key.lstrip("-*+> ").strip()
        if len(body) < min_len or key.startswith(("#", "|")):
            out.append(line)
            continue
        if body in seen:
            continue
        seen.add(body)
        out.append(line)
    return "\n".join(out)


def strip_emphasis(text: str) -> str:
    """Remove bold/italic markers. Each ``**`` pair is 1-2 tokens of styling."""
    text = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", text)
    return text


def strip_horizontal_rules(text: str) -> str:
    """Remove ``---`` / ``***`` separator lines."""
    return re.sub(r"\n[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*(?=\n)", "", text)


def squeeze_headings(text: str) -> str:
    """Remove the blank line that follows a heading."""
    return re.sub(r"(\n#{1,6} [^\n]*)\n\n+", r"\1\n", text)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Pass:
    name: str
    fn: Callable[[str], str]
    aggressive: bool
    why: str


PASSES: tuple[Pass, ...] = (
    Pass("unicode", normalize_unicode, False, "typographic chars -> cheaper ASCII"),
    Pass("html-comments", strip_html_comments, False, "drop <!-- --> comments"),
    Pass("trailing-ws", strip_trailing_whitespace, False, "drop trailing spaces/tabs"),
    Pass("spaces", collapse_spaces, False, "collapse runs of spaces in a line"),
    Pass("blank-lines", collapse_blank_lines, False, "3+ newlines -> one blank line"),
    Pass("xml-gaps", tighten_xml, False, "drop blank lines between tags"),
    Pass("list-gaps", collapse_list_gaps, False, "drop blank lines between list items"),
    Pass("trim", trim_document, False, "strip document head/tail whitespace"),
    Pass("hrules", strip_horizontal_rules, True, "remove --- separators"),
    Pass("headings", squeeze_headings, True, "drop blank line after headings"),
    Pass("emphasis", strip_emphasis, True, "remove ** and * styling markers"),
    Pass("dedupe", dedupe_lines, True, "drop repeated long lines"),
)

PASSES_BY_NAME = {p.name: p for p in PASSES}


@dataclass
class PassResult:
    name: str
    why: str
    tokens_before: int
    tokens_after: int

    @property
    def saved(self) -> int:
        return self.tokens_before - self.tokens_after


@dataclass
class SlimResult:
    """Outcome of a compression run, with per-pass attribution."""

    original: str
    slimmed: str
    before: CountResult
    after: CountResult
    passes: list[PassResult] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return self.before.tokens - self.after.tokens

    @property
    def pct(self) -> float:
        if not self.before.tokens:
            return 0.0
        return self.saved / self.before.tokens * 100

    @property
    def effective_passes(self) -> list[PassResult]:
        """Only the passes that actually removed tokens, biggest win first."""
        return sorted((p for p in self.passes if p.saved > 0), key=lambda p: -p.saved)


def slim(
    text: str,
    *,
    aggressive: bool = False,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
    model: str = "claude-opus-5",
    exact: bool = False,
) -> SlimResult:
    """Compress ``text`` and report the token savings.

    Args:
        aggressive: Also run passes that change rendered content.
        only: Run just these passes, by name, in registry order.
        exclude: Skip these passes by name.
        exact: Measure with the API rather than the offline estimator.

    Returns:
        A :class:`SlimResult`. ``result.slimmed`` is the compressed text.
    """
    unknown = set(only or []) | set(exclude or [])
    unknown -= set(PASSES_BY_NAME)
    if unknown:
        raise ValueError(
            f"unknown pass(es): {', '.join(sorted(unknown))}. "
            f"available: {', '.join(PASSES_BY_NAME)}"
        )

    selected = [
        p
        for p in PASSES
        if (aggressive or not p.aggressive)
        and (only is None or p.name in only)
        and (exclude is None or p.name not in exclude)
    ]

    protected, stash = _protect(text)
    before = count(text, model=model, exact=exact)

    results: list[PassResult] = []
    current = protected
    for p in selected:
        # Measure each pass on the restored text so reported savings are real
        # token deltas on real output, not deltas on sentinel-laden text.
        prev_tokens = count(_restore(current, stash), model=model, exact=False).tokens
        nxt = p.fn(current)
        next_tokens = count(_restore(nxt, stash), model=model, exact=False).tokens
        results.append(PassResult(p.name, p.why, prev_tokens, next_tokens))
        current = nxt

    slimmed = _restore(current, stash)
    after = count(slimmed, model=model, exact=exact)
    return SlimResult(
        original=text, slimmed=slimmed, before=before, after=after, passes=results
    )
