"""
Embedding — turn chunk text into dense vectors for pgvector retrieval.

One async entry point, ``embed(texts) -> list[vector]``, returning a 3072-dim
float vector per input (OpenAI ``text-embedding-3-large``, stored as
``halfvec(3072)``). We call it through OpenRouter's OpenAI-compatible
``/embeddings`` route by default so every model call uses the same key; set
``EMBED_BASE_URL`` to ``https://api.openai.com/v1`` to talk to OpenAI directly if
the gateway doesn't proxy embeddings.

Deliberately thin: raw httpx (already a dependency) instead of the OpenAI SDK,
input batched to keep request count low, oversized inputs token-truncated to the
model's limit, and a small retry on transient (429 / 5xx / network) errors.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

# Allow running this file directly (`python embed.py` from app/embed/), not just
# as `python -m app.embed.embed`: when there's no package context, put the
# backend root on sys.path so `app.*` imports resolve.
if __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app import apikey
from app.config import settings

logger = logging.getLogger(__name__)

EMBED_DIM = 3072            # text-embedding-3-large native dimensionality

_BATCH_SIZE = 96           # inputs per request — well under provider array limits
_MAX_INPUT_TOKENS = 8000   # model hard cap is ~8192; leave headroom
_MAX_RETRIES = 4
_TIMEOUT = httpx.Timeout(60.0)


class EmbedError(RuntimeError):
    """Raised when the embedding endpoint fails or returns an unexpected shape."""


# Lazy tiktoken encoder, shared, only built if we actually need to truncate.
_encoder = None


def _truncate(text: str) -> str:
    """Clip a single input to the model's token limit. A normal chunk is far
    smaller; this only bites on a pathological input (e.g. one giant equation),
    which we'd rather embed truncated than have the whole batch rejected."""
    global _encoder
    if _encoder is None:
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("o200k_base")
        except Exception:  # noqa: BLE001 - fall back to a coarse char clip
            _encoder = False
    if _encoder is False:
        return text[: _MAX_INPUT_TOKENS * 4]
    tokens = _encoder.encode(text)
    if len(tokens) <= _MAX_INPUT_TOKENS:
        return text
    logger.warning("embed: truncating input from %d to %d tokens", len(tokens), _MAX_INPUT_TOKENS)
    return _encoder.decode(tokens[:_MAX_INPUT_TOKENS])


async def _embed_batch(client: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
    """POST one batch, with retry/backoff, and return vectors in input order."""
    payload = {"model": settings.embed_model, "input": batch, "dimensions": EMBED_DIM}
    headers = {"Authorization": f"Bearer {apikey.current_key()}"}
    url = f"{settings.embed_base_url.rstrip('/')}/embeddings"

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise EmbedError(f"transient {resp.status_code}: {resp.text[:200]}")
            if resp.status_code != 200:
                raise EmbedError(f"embeddings request failed ({resp.status_code}): {resp.text[:300]}")
            # Sort by index — the API may return rows out of order.
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vectors = [row["embedding"] for row in data]
            if len(vectors) != len(batch) or any(len(v) != EMBED_DIM for v in vectors):
                raise EmbedError("embeddings response shape mismatch")
            return vectors
        except (httpx.HTTPError, EmbedError, KeyError) as exc:
            if attempt == _MAX_RETRIES:
                raise EmbedError(f"embedding failed after {attempt} attempts: {exc}") from exc
            backoff = 2 ** (attempt - 1)
            logger.warning("embed: attempt %d/%d failed (%s); retrying in %ss",
                           attempt, _MAX_RETRIES, exc, backoff)
            await asyncio.sleep(backoff)
    raise EmbedError("unreachable")  # pragma: no cover


async def embed(texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` into ``EMBED_DIM``-dim vectors, batched and in order.

    Args:
        texts: chunk contents to embed (typically each chunk's ``content_full``).

    Returns:
        One vector per input, same order. Empty input -> empty list.

    Raises:
        EmbedError: on a non-recoverable endpoint failure or bad response shape.
    """
    if not texts:
        return []
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = [_truncate(t) for t in texts[i : i + _BATCH_SIZE]]
            vectors.extend(await _embed_batch(client, batch))
    return vectors


# ---------------------------------------------------------------------------
# Manual test harness:  python -m app.embed.embed
# Embeds a few strings, checks dims, and prints cosine similarities so you can
# eyeball that related sentences score higher than unrelated ones — and confirm
# the configured endpoint actually serves embeddings.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    def _cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    samples = [
        "Backpropagation computes gradients via the chain rule.",
        "Gradients flow backward through the network during training.",
        "The cat sat lazily on the warm windowsill.",
    ]
    try:
        vecs = asyncio.run(embed(samples))
    except EmbedError as exc:
        raise SystemExit(f"\nEmbedding call failed: {exc}\n"
                         f"(base_url={settings.embed_base_url}, model={settings.embed_model})")

    print(f"got {len(vecs)} vectors of dim {len(vecs[0])} (expected {EMBED_DIM})")
    print(f"related   (0,1): {_cos(vecs[0], vecs[1]):.3f}")
    print(f"unrelated (0,2): {_cos(vecs[0], vecs[2]):.3f}")
    sys.exit(0)
