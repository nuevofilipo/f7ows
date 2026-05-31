"""
LLM chat helper — a thin async wrapper over the OpenRouter chat-completions API.

Mirrors embed.py: raw httpx, one key, base URL/model from settings. Used so far
only by the retrieval router's "planner" step, but kept general (and reusable by
the future chat/slash-command features).
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when the chat-completions endpoint fails or returns no content."""


async def complete(
    messages: list[dict],
    *,
    model: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    top_p: float | None = None,
    top_k: int | None = None,
    timeout: float = 60.0,
) -> str:
    """Run a chat completion and return the assistant's text content."""
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model or settings.planner_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    # Optional sampling controls — only sent when set (top_k=0 means "off").
    if top_p is not None:
        payload["top_p"] = top_p
    if top_k:
        payload["top_k"] = top_k
    headers = {"Authorization": f"Bearer {settings.openrouter_key}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise LLMError(f"chat request failed ({resp.status_code}): {resp.text[:300]}")
            content = resp.json()["choices"][0]["message"]["content"]
            # Reasoning models can return 200 with empty content when max_tokens is
            # too small (the budget is spent on reasoning) — treat that as an error
            # rather than silently passing None/"" downstream.
            if not content:
                raise LLMError("empty completion (max_tokens may be too small for a reasoning model)")
            return content
    except (httpx.HTTPError, KeyError, IndexError) as exc:
        raise LLMError(f"chat completion failed: {exc}") from exc
