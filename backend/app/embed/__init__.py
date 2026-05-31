"""Batched embedding of chunk text into halfvec(3072) vectors."""
from app.embed.embed import EMBED_DIM, EmbedError, embed

__all__ = ["EMBED_DIM", "EmbedError", "embed"]
