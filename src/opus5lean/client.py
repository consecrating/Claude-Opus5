"""Minimal API clients, stdlib only.

Two backends:

* :class:`AnthropicClient` -- the real thing. Sets ``output_config.effort``,
  reads back ``usage`` including cache counters, and selects content blocks by
  ``type`` rather than position (mandatory on Opus 5: thinking is on by
  default, so ``content[0]`` is frequently a thinking block, not text).
* :class:`OpenAICompatClient` -- any OpenAI-shaped endpoint. This exists so
  the structural parts of your work (does the prompt parse, does the tool loop
  terminate, does the output validate) can be exercised on a free provider
  before you spend anything on Opus 5. See docs/FREE-API-KEYS.md.

``urllib`` rather than ``httpx`` keeps the package dependency-free, so the
offline tools work in any environment with no install step.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .pricing import DEFAULT_EFFORT, EFFORT_LEVELS, Usage
from .tokens import resolve_api_model

ANTHROPIC_MESSAGES = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class ApiError(RuntimeError):
    """An API call failed."""


@dataclass
class Response:
    """Normalised result of one completion, whichever backend produced it."""

    text: str
    usage: Usage
    model: str
    effort: str | None = None
    latency_s: float = 0.0
    thinking_chars: int = 0
    stop_reason: str | None = None
    raw: dict = field(default_factory=dict)


def _post(url: str, payload: dict, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:600]
        raise ApiError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except Exception as exc:
        raise ApiError(f"request to {url} failed: {exc}") from exc


class AnthropicClient:
    """Claude Messages API client with effort and cache reporting."""

    def __init__(self, api_key: str | None = None, timeout: float = 600.0):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        if not self.api_key:
            raise ApiError("ANTHROPIC_API_KEY is not set")
        self.timeout = timeout

    def complete(
        self,
        prompt: str,
        *,
        model: str = "claude-opus-5",
        system: str | None = None,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = 8192,
        cache_system: bool = False,
        thinking_disabled: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> Response:
        """Send one message and return a normalised :class:`Response`.

        Args:
            effort: One of :data:`~opus5lean.pricing.EFFORT_LEVELS`.
            cache_system: Put a ``cache_control`` breakpoint on the system
                prompt. Only worthwhile if the system prompt clears the
                model's minimum cacheable length.
            thinking_disabled: Ask for no thinking. Opus 5 rejects this at
                ``xhigh`` and ``max`` with a 400, so it is refused locally
                with a clearer message.
        """
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {EFFORT_LEVELS}, got {effort!r}")
        if thinking_disabled and effort in ("xhigh", "max"):
            raise ValueError(
                f"Opus 5 returns 400 for thinking:disabled at effort={effort}. "
                "Either drop to effort 'high' or below, or keep thinking enabled "
                "and lower effort to control cost."
            )

        payload: dict = {
            "model": resolve_api_model(model),
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": effort},
        }
        if system is not None:
            if cache_system:
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            else:
                payload["system"] = system
        if thinking_disabled:
            payload["thinking"] = {"type": "disabled"}

        headers = {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if extra_headers:
            headers.update(extra_headers)

        start = time.perf_counter()
        body = _post(ANTHROPIC_MESSAGES, payload, headers, self.timeout)
        latency = time.perf_counter() - start
        return self._parse(body, model=model, effort=effort, latency=latency)

    @staticmethod
    def _parse(body: dict, *, model: str, effort: str, latency: float) -> Response:
        # Select by block type. On Opus 5 a response commonly opens with one or
        # more thinking blocks, so indexing content[0] is a bug.
        texts: list[str] = []
        thinking_chars = 0
        for block in body.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype in ("thinking", "redacted_thinking"):
                thinking_chars += len(block.get("thinking") or "")

        u = body.get("usage", {}) or {}
        usage = Usage(
            input_tokens=int(u.get("input_tokens", 0) or 0),
            output_tokens=int(u.get("output_tokens", 0) or 0),
            cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
        )
        return Response(
            text="".join(texts),
            usage=usage,
            model=model,
            effort=effort,
            latency_s=latency,
            thinking_chars=thinking_chars,
            stop_reason=body.get("stop_reason"),
            raw=body,
        )


class OpenAICompatClient:
    """Client for any OpenAI-compatible ``/chat/completions`` endpoint.

    Use this to shake out prompt structure on a free provider before paying
    for Opus 5 runs. Effort is not part of the OpenAI schema, so it is
    recorded on the response for reporting but not sent.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 300.0,
    ):
        self.base_url = (base_url or os.environ.get("OPENAI_COMPAT_BASE_URL") or "").rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_COMPAT_API_KEY") or ""
        self.model = model or os.environ.get("OPENAI_COMPAT_MODEL") or ""
        if not self.base_url or not self.api_key or not self.model:
            raise ApiError(
                "set OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_API_KEY and "
                "OPENAI_COMPAT_MODEL (see .env.example and docs/FREE-API-KEYS.md)"
            )
        self.timeout = timeout

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> Response:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
        }
        start = time.perf_counter()
        body = _post(f"{self.base_url}/chat/completions", payload, headers, self.timeout)
        latency = time.perf_counter() - start

        choices = body.get("choices") or [{}]
        text = ((choices[0].get("message") or {}).get("content")) or ""
        u = body.get("usage", {}) or {}
        usage = Usage(
            input_tokens=int(u.get("prompt_tokens", 0) or 0),
            output_tokens=int(u.get("completion_tokens", 0) or 0),
        )
        return Response(
            text=text,
            usage=usage,
            model=payload["model"],
            latency_s=latency,
            stop_reason=choices[0].get("finish_reason"),
            raw=body,
        )


def auto_client() -> AnthropicClient | OpenAICompatClient:
    """Return an Anthropic client if a key is set, else an OpenAI-compatible one."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient()
    return OpenAICompatClient()
