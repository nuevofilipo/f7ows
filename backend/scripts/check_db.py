"""Ad-hoc verification of schema state."""
import asyncio
import asyncpg
from app.config import settings


async def main() -> None:
    conn = await asyncpg.connect(settings.postgres_url)
    try:
        col = await conn.fetchrow("""
            SELECT format_type(atttypid, atttypmod) AS col_type
            FROM pg_attribute
            WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'
        """)
        print("chunks.embedding type:", col["col_type"])

        idx = await conn.fetch("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname='public' AND tablename='chunks'
            ORDER BY indexname
        """)
        for r in idx:
            print(f"  {r['indexname']}: {r['indexdef']}")

        version = await conn.fetchval("SELECT version_num FROM alembic_version")
        print("Alembic version:", version)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
