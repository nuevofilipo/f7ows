"""
SQLite connection — imported by the FastAPI lifespan and the worker.

One connection per process (the web app has one, the worker has one), both
against the same file in the data directory. That is not a downgrade from the old
asyncpg pool: SQLite serialises writes anyway, so a pool would mostly buy threads
that queue behind each other. Cross-process concurrency is handled by WAL plus
``busy_timeout``, which lets the worker write while the web app reads.

The module keeps the names the rest of the app already imports —
``create_pool`` / ``close_pool`` / ``get_pool`` / ``acquire`` — so main.py and
worker.py don't change.

``acquire()`` hands back a :class:`Connection`, a thin adapter that speaks
asyncpg's API (``fetch`` / ``fetchrow`` / ``fetchval`` / ``execute`` /
``executemany`` / ``transaction``) over aiosqlite. That is deliberate: it keeps
the port to the SQL itself rather than restructuring all ~43 call sites, so the
diff stays reviewable. What it does *not* do is rewrite ``$1`` placeholders into
``?`` — a regex for that works right up until a parameter is used twice, and it
would hide the change from review. Those are converted by hand, visibly.

Rows come back as :class:`Row`, a dict subclass that also supports positional
access. ``sqlite3.Row`` was tempting but is a trap here: ``"key" in row``
iterates the row's *values*, so auth.py's ``"openrouter_key" in row`` would
quietly evaluate False and the user's key would never load.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Iterable, Sequence
from uuid import UUID

import aiosqlite
import sqlite_vec

from app import paths
# NOW is re-exported: it belongs to the schema (it is the column default), but
# the routers that need the same expression have no business importing the
# migration module, so they get it from here alongside the rest of the SQL bits.
from app.migrations import NOW, migrate  # noqa: F401  (re-export)

logger = logging.getLogger(__name__)

# Bind uuid.UUID parameters as their canonical text form. Postgres had a native
# uuid type; SQLite does not, so ids are TEXT and every UUID handed to a query
# would otherwise raise "type 'UUID' is not supported". Registering the adapter
# once here means call sites can keep passing whichever of UUID/str they already
# had — the alternative was a str() at forty-odd argument positions, each one a
# chance to miss one and get a runtime error on a rarely-hit route.
#
# Note the asymmetry: ids come back *out* of the database as str, never UUID.
# That matches what the API already emitted (FastAPI serialised asyncpg's UUIDs
# to strings anyway), so responses are unchanged.
sqlite3.register_adapter(UUID, str)


# ---------------------------------------------------------------------------
# SQL fragments
#
# Three things Postgres did natively have no SQLite equivalent, and each is
# needed in more than one module. Building them here keeps the shape of the
# translation in one reviewable place rather than smeared across the routers.
# ---------------------------------------------------------------------------

def placeholders(values: Sequence[Any]) -> str:
    """``"?, ?, ?"`` — the body of an ``IN`` list.

    SQLite has no array parameter, so asyncpg's ``= ANY($1::uuid[])`` cannot be
    bound as one argument; the list has to be expanded into the SQL text and its
    elements passed as individual parameters.
    """
    return ", ".join("?" * len(values))


def serialize_vector(values: Sequence[float]) -> bytes:
    """Pack an embedding into the little-endian float32 blob sqlite-vec speaks.

    Needed on both sides of the wire: it is what a ``vec0`` column stores, and
    what ``vec_distance_cosine()`` expects as its query argument. pgvector did
    this conversion inside its own type; here it is explicit.
    """
    return sqlite_vec.serialize_float32(list(values))


def descendants_of(column: str, prefixes: Sequence[str]) -> tuple[str, list[str]]:
    """ltree ``column <@ ANY(prefixes)`` — "at or below any of these paths".

    Returns the SQL fragment and the parameters it consumes, in order.

    GLOB rather than LIKE, deliberately. Chunk path labels are slugified to
    ``[a-z0-9_]`` (app/chunk/chunk.py), and ``_`` is a single-character wildcard
    in LIKE — so ``LIKE 'self_attention.%'`` would also match a sibling branch
    named ``selfxattention``. GLOB's metacharacters are ``*``, ``?``, ``[`` and
    ``]``, none of which a slugified label or a zero-padded message label can
    contain, so the pattern is exact.

    The ``= ?`` arm is not redundant: ``<@`` includes the prefix row itself,
    which ``GLOB 'p.*'`` does not match.
    """
    if not prefixes:
        return "0", []          # matches nothing, without a syntax error
    clause = " OR ".join(f"({column} = ? OR {column} GLOB ? || '.*')" for _ in prefixes)
    return f"({clause})", [p for prefix in prefixes for p in (prefix, prefix)]


def ancestors_of(column: str, path: str) -> tuple[str, list[str]]:
    """ltree ``column @> path`` — "at or above this path", i.e. the branch from
    the root down to ``path``. The mirror image of :func:`descendants_of`."""
    return f"({column} = ? OR ? GLOB {column} || '.*')", [path, path]



_conn: aiosqlite.Connection | None = None
_wrapper: "Connection | None" = None
# Serialises access within this process. One connection means two tasks could
# otherwise interleave statements — and worse, a write from task B could land
# inside task A's open transaction. Every acquire() holds this for its block,
# which is safe because no acquire() block in this codebase awaits anything slow
# (no LLM or network call happens while holding one).
_lock: asyncio.Lock | None = None


class Row(dict):
    """A result row: ``row["name"]`` and ``row[0]`` both work, and ``in`` tests
    column names the way asyncpg's Record does."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor: sqlite3.Cursor, values: tuple) -> Row:
    return Row(zip((c[0] for c in cursor.description), values))


class Connection:
    """asyncpg-shaped facade over one aiosqlite connection."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def execute(self, sql: str, *args: Any):
        """Run one statement. Returns the cursor, so callers can read
        ``lastrowid`` — which the ingest path needs to link a freshly inserted
        chunk to its vector."""
        return await self._db.execute(sql, args)

    async def executemany(self, sql: str, args: Iterable[Sequence[Any]]):
        return await self._db.executemany(sql, args)

    async def fetch(self, sql: str, *args: Any) -> list[Row]:
        cursor = await self._db.execute(sql, args)
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    async def fetchrow(self, sql: str, *args: Any) -> Row | None:
        cursor = await self._db.execute(sql, args)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def fetchval(self, sql: str, *args: Any) -> Any:
        row = await self.fetchrow(sql, *args)
        return None if row is None else row[0]

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None, None]:
        """``async with conn.transaction():`` — same shape as asyncpg's.

        BEGIN IMMEDIATE, not a plain BEGIN: a deferred transaction takes its
        write lock lazily, so two processes that both start reading and then both
        try to write deadlock instead of queueing. Taking the lock up front makes
        the loser wait out its busy_timeout and proceed.
        """
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            await self._db.execute("ROLLBACK")
            raise
        else:
            await self._db.execute("COMMIT")


async def create_pool() -> "Connection":
    """Open the connection, load sqlite-vec, apply pragmas, run migrations.

    Named for the asyncpg function it replaces so the lifespan hooks in main.py
    and worker.py are untouched.
    """
    global _conn, _wrapper, _lock

    path = paths.db_path()
    fresh = not path.exists()
    logger.info("db: opening %s%s", path, " (new)" if fresh else "")

    # isolation_level=None means autocommit: the driver stops opening implicit
    # transactions and Connection.transaction() controls them explicitly. It has
    # to be passed here — assigning db.isolation_level afterwards deadlocks
    # aiosqlite's connection thread.
    _conn = await aiosqlite.connect(path, isolation_level=None)
    _conn.row_factory = _row_factory

    # sqlite-vec ships a small native library (~289 KB) that has to be loaded
    # into each connection before any vec0 table can be touched. Loading runs on
    # aiosqlite's own thread, hence the _execute hop.
    await _conn.enable_load_extension(True)
    await _conn._execute(sqlite_vec.load, _conn._conn)
    await _conn.enable_load_extension(False)

    for pragma in (
        "journal_mode=WAL",      # readers don't block the writer, and vice versa
        "foreign_keys=ON",       # OFF by default; ON DELETE CASCADE is a no-op without it
        "busy_timeout=5000",     # wait out the other process instead of failing
        "synchronous=NORMAL",    # safe under WAL, much faster than FULL
    ):
        await _conn.execute(f"PRAGMA {pragma}")

    await migrate(_conn)

    _lock = asyncio.Lock()
    _wrapper = Connection(_conn)
    return _wrapper


async def close_pool() -> None:
    global _conn, _wrapper, _lock
    if _conn is not None:
        # Fold the WAL back into the main file so a copy of db.sqlite3 alone is a
        # complete database — this is what makes "back up one file" honest.
        try:
            await _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 - never let cleanup mask a real shutdown error
            logger.warning("db: wal checkpoint on close failed", exc_info=True)
        await _conn.close()
    _conn, _wrapper, _lock = None, None, None


def get_pool() -> "Connection":
    if _wrapper is None:
        raise RuntimeError("Database is not initialised. Call create_pool() first.")
    return _wrapper


@asynccontextmanager
async def acquire() -> AsyncGenerator[Connection, None]:
    """Usage: ``async with acquire() as conn: ...``

    Unchanged from the asyncpg version at every call site. The difference is that
    there is one connection rather than a pool, so this serialises on a lock for
    the duration of the block instead of handing out a spare connection.
    """
    conn = get_pool()
    assert _lock is not None
    async with _lock:
        yield conn


# ---------------------------------------------------------------------------
# Harness:  python -m app.db
#
# Opens (creating if needed) the real dev database, reports where it is and what
# state it's in, and round-trips a row through the adapter.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    async def _main() -> None:
        print(paths.describe(), "\n")
        conn = await create_pool()
        try:
            version = await conn.fetchval("PRAGMA user_version")
            journal = await conn.fetchval("PRAGMA journal_mode")
            fks = await conn.fetchval("PRAGMA foreign_keys")
            vec = await conn.fetchval("SELECT vec_version()")
            print(f"schema version {version} | journal={journal} | "
                  f"foreign_keys={fks} | sqlite-vec {vec}")

            rows = await conn.fetch(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")
            print("tables:", ", ".join(r["name"] for r in rows))

            counts = []
            for table in ("users", "projects", "documents", "chunks", "messages", "jobs"):
                counts.append(f"{table}={await conn.fetchval(f'SELECT count(*) FROM {table}')}")
            print("rows:", "  ".join(counts))
        finally:
            await close_pool()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_main())
