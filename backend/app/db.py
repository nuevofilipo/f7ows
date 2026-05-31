"""
asyncpg connection pool — imported by FastAPI lifespan and ARQ workers.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None
# what even is this pool?

async def create_pool() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        dsn=settings.postgres_url,
        min_size=2,
        max_size=10,
        init=_init_connection,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialised. Call create_pool() first.")
    return _pool


# what is the purpose of this acquire function? why does one thing need to acquire it?
@asynccontextmanager
async def acquire() -> AsyncGenerator[asyncpg.Connection, None]:
    """Usage: `async with acquire() as conn: ...`"""
    async with get_pool().acquire() as conn:
        yield conn


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register pgvector codec on each new connection in the pool."""
    try:
        from pgvector.asyncpg import register_vector
        await register_vector(conn)
    except Exception:
        # pgvector extension may not be installed yet (e.g. before first migration)
        pass
