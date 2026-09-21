"""
Embedding — turn chunk text into dense vectors for sqlite-vec retrieval.

One async entry point, ``embed(texts) -> list[vector]``, returning an
``EMBED_DIM``-wide float vector per input (OpenAI ``text-embedding-3-large``,
truncated by the API's ``dimensions`` parameter). We call it through
OpenRouter's OpenAI-compatible
``/embeddings`` route by default so every model call uses the same key. The
endpoint is configurable per role (:mod:`app.providers`) — point the embed role
at ``https://api.openai.com/v1`` to talk to OpenAI directly if the gateway
doesn't proxy embeddings.

Changing the embed *model* is not a free switch: vectors from two models are not
comparable and the schema fixes the width, so an existing project would need
re-embedding. That is the deferred work in TODO_desktop.md phase 5, not something
this setting does for you.

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

from app import providers, tokens
from app.migrations import EMBED_DIM

logger = logging.getLogger(__name__)

# Vector width. Owned by app/migrations.py because the schema is the binding
# constraint (a vec0 column's dimension is fixed when the table is created), and
# a mismatch here would write vectors the database cannot store.
# text-embedding-3-large is natively 3072 but Matryoshka-trained, so asking the
# API for 1024 via `dimensions` truncates *and re-normalises* server-side, at a
# quality cost of ~1-2% MTEB. Worth it: sqlite-vec has no float16, so 1024
# float32 is 4KB/vector against 12KB at 3072 — a third of the disk and a third
# of every brute-force scan.

_BATCH_SIZE = 96           # inputs per request — well under provider array limits
# Model hard cap is ~8192. The headroom absorbs the token estimate's error: the
# estimate is a corpus mean, and LaTeX-dense text tokenizes ~10% worse than that,
# so 8000 could still land over the cap and get the whole batch rejected. Only
# pathological inputs are clipped at all, so the spare 1200 costs us nothing.
_MAX_INPUT_TOKENS = 7000
_MAX_RETRIES = 4
_TIMEOUT = httpx.Timeout(60.0)


class EmbedError(RuntimeError):
    """Raised when the embedding endpoint fails or returns an unexpected shape."""


def _truncate(text: str) -> str:
    """Clip a single input to the model's token limit. A normal chunk is far
    smaller; this only bites on a pathological input (e.g. one giant equation),
    which we'd rather embed truncated than have the whole batch rejected."""
    clipped = tokens.clip(text, _MAX_INPUT_TOKENS)
    if clipped is not text:
        logger.warning("embed: truncating input from %d to %d chars (~%d tokens)",
                       len(text), len(clipped), _MAX_INPUT_TOKENS)
    return clipped


async def _embed_batch(client: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
    """POST one batch, with retry/backoff, and return vectors in input order."""
    provider = providers.for_role("embed")
    payload = {"model": provider.model, "input": batch, "dimensions": EMBED_DIM}
    headers = provider.auth_header()
    url = provider.url("/embeddings")

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
