"""Sanity check: connect to Railway Redis via ARQ and PING."""
import asyncio
from arq import create_pool
from arq.connections import RedisSettings
from app.config import settings


async def main() -> None:
    pool = await create_pool(RedisSettings.from_dsn(settings.redis_public_url))
    pong = await pool.ping()
    print("Redis PING:", pong)
    await pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
