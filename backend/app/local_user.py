"""
The single local user.

The cloud app was multi-tenant; the desktop app has exactly one person using it.
The ``users`` table stays anyway — it is the FK root every project hangs off, and
dropping it would mean rewriting every ownership check for no gain. Instead we
seed one row on first launch and treat every request as that user
(``auth.get_current_user``), so there is no login screen.

Deliberately *not* a schema migration: migrations describe shape, and this is
data. It also has to be idempotent across restarts and safe when the web process
and the worker both start at once, which a one-shot migration is not.
"""
from __future__ import annotations

import logging
import secrets
from uuid import uuid4

from app.config import settings
from app.db import acquire

logger = logging.getLogger(__name__)

_cached_id: str | None = None


async def ensure_local_user() -> str:
    """Return the local user's id, creating the row on first launch.

    Idempotent, and safe if two processes call it at the same moment: the insert
    is ``ON CONFLICT (email) DO NOTHING`` and the id is re-read afterwards, so
    the loser of the race adopts the winner's row instead of creating a second.
    """
    global _cached_id
    if _cached_id is not None:
        return _cached_id

    email = settings.single_user_email.strip().lower()
    async with acquire() as conn:
        existing = await conn.fetchval("SELECT id FROM users WHERE email = ?", email)
        if existing is None:
            # The password is never used — there is no login screen — but the
            # column is NOT NULL and a fixed or empty value would become a real
            # credential the moment someone re-enables the login route.
            from app.auth.auth import hash_password

            await conn.execute(
                "INSERT INTO users (id, email, hashed_password) VALUES (?, ?, ?) "
                "ON CONFLICT (email) DO NOTHING",
                uuid4(), email, hash_password(secrets.token_urlsafe(32)),
            )
            existing = await conn.fetchval("SELECT id FROM users WHERE email = ?", email)
            logger.info("local_user: seeded %s (%s)", email, existing)

    _cached_id = str(existing)
    return _cached_id
