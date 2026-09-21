"""Batched embedding of chunk text into float32 vectors (see app.migrations.EMBED_DIM)."""
from app.embed.embed import EMBED_DIM, EmbedError, embed

__all__ = ["EMBED_DIM", "EmbedError", "embed"]
