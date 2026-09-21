"""
LLM chat helper — a thin async wrapper over an OpenAI-compatible
chat-completions API.

Mirrors embed.py: raw httpx, with the endpoint and credential resolved per
*role* (:mod:`app.providers`). The role decides where the call goes, so the cheap
planner can run against a local Ollama while chat stays on a frontier model,
without either call site knowing anything about providers.
"""
from __future__ import annotations

import logging

import httpx

from app import cost, providers

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
    role: str = "planner",
) -> str:
    """Run a chat completion and return the assistant's text content.

    ``role`` selects the provider — "planner" by default, since the cheap
    classifier calls are exactly the ones that omit it. ``model`` still wins over
    the role's configured model, which is how the user's chat-model picker works.
    """
    provider = providers.for_role(role)
    url = provider.url("/chat/completions")
    payload = {
        "model": model or provider.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Ask OpenRouter to return the real dollar cost of this generation in the
        # response `usage.cost`, which we accumulate per request (see app.cost).
        # Other providers ignore the field and report nothing; app.cost counts
        # those calls as unpriced rather than pretending they were free.
        "usage": {"include": True},
    }
    # Optional sampling controls — only sent when set (top_k=0 means "off").
    if top_p is not None:
        payload["top_p"] = top_p
    if top_k:
        payload["top_k"] = top_k
    headers = provider.auth_header()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise LLMError(f"chat request failed ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            # Record the real dollar cost (we were charged even if content is empty).
            cost.add((data.get("usage") or {}).get("cost"))
            content = data["choices"][0]["message"]["content"]
            # Reasoning models can return 200 with empty content when max_tokens is
            # too small (the budget is spent on reasoning) — treat that as an error
            # rather than silently passing None/"" downstream.
            if not content:
                raise LLMError("empty completion (max_tokens may be too small for a reasoning model)")
            return content
    except (httpx.HTTPError, KeyError, IndexError) as exc:
        raise LLMError(f"chat completion failed: {exc}") from exc


async def list_models(*, timeout: float = 15.0) -> list[dict]:
    """Fetch the catalogue of models available on the gateway (the OpenAI-shaped
    ``/models``). Returns the raw ``data`` list; the caller trims it.

    Asks the *chat* role's provider, since this feeds the chat model picker — on
    a local Ollama that correctly lists the models pulled on this machine rather
    than OpenRouter's catalogue.
    """
    provider = providers.for_role("chat")
    url = provider.url("/models")
    headers = provider.auth_header()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                raise LLMError(f"models request failed ({resp.status_code}): {resp.text[:300]}")
            data = resp.json().get("data")
            if not isinstance(data, list):
                raise LLMError("models response missing a 'data' list")
            return data
    except (httpx.HTTPError, ValueError) as exc:
        raise LLMError(f"listing models failed: {exc}") from exc
