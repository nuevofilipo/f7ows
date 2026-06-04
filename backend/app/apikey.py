"""
Per-context OpenRouter key — the BYOK plumbing.

Every model call (chat, planner, embeddings) goes through one OpenRouter key. By
default that's the global key from env; when a user has stored their own key, all
of *their* traffic should use it instead. Rather than thread a ``key=`` argument
through every function (retrieval router, planner, generate, embed, ...), we keep
the active key in a :class:`contextvars.ContextVar`, mirroring :mod:`app.trace`.

- HTTP requests: :func:`set_current_encrypted` is called once in
  ``get_current_user``. Each request runs in its own contextvars context, so the
  value is isolated per request and is copied into any ``asyncio.create_task``
  spawned during it (e.g. background deepsummary), which is exactly what we want.
- Worker jobs: a long-lived process runs many jobs in one task, so use the
  :func:`using_encrypted` context manager to set *and reset* around a block.

Consumers call :func:`current_key`, which falls back to the global env key when no
per-user key is in context — so nothing breaks for users who haven't set one.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from app import crypto
from app.config import settings

# The decrypted per-user key for the active context, or None to use the global key.
_current: ContextVar[str | None] = ContextVar("openrouter_key", default=None)


def current_key() -> str:
    """The OpenRouter key for the active context: the user's BYOK key if set, else
    the global fallback from env."""
    return _current.get() or settings.openrouter_key


def set_current_encrypted(enc: str | None) -> None:
    """Decrypt and install the per-request user key. No reset: safe because each
    HTTP request runs in its own contextvars context (and create_task copies it)."""
    _current.set(crypto.decrypt(enc))


@contextmanager
def using_encrypted(enc: str | None):
    """Scope a per-user key around a block (for the worker, where one task handles
    many jobs and must not leak a key between them)."""
    token = _current.set(crypto.decrypt(enc))
    try:
        yield
    finally:
        _current.reset(token)
