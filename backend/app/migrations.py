"""
Database schema and migrations.

Replaces Alembic. A desktop app has to migrate itself at startup — the user is
never going to run ``alembic upgrade head`` — so the migration machinery has to
be embeddable and dependency-free. SQLite gives us exactly the hook for that:
``PRAGMA user_version``, a 4-byte integer in the database header that SQLite
itself never touches and that is reserved for the application.

The scheme is a numbered list. Each migration is a tuple of SQL statements; the
runner reads ``user_version``, applies everything above it in one transaction per
migration, and bumps the number. Alembic's autogenerate was never used here (all
three of the old migrations were hand-written ``op.execute`` blocks), so nothing
of value is lost.

Rules for adding a migration:

* Append a new ``(version, statements)`` entry. **Never edit a shipped one** —
  users' databases have already run it.
* One statement per string. Do not concatenate with ``;`` and split later: a
  ``CREATE TRIGGER`` body contains semicolons of its own.
* SQLite can only ``ALTER TABLE`` in limited ways (add column, rename, drop
  column). Anything else is the 12-step dance: create the new table, copy, drop
  the old, rename.

Two things the connection must have before this runs (see app/db.py):

* the **sqlite-vec extension loaded**, or ``CREATE VIRTUAL TABLE ... USING vec0``
  fails;
* ``isolation_level=None`` passed to ``aiosqlite.connect()``, so the BEGIN/COMMIT
  in :func:`migrate` are honoured. It must be set at connect time — assigning
  ``db.isolation_level`` afterwards deadlocks aiosqlite's connection thread.

``PRAGMA foreign_keys=ON`` is also required, but that's for the cascades, not for
this module. (Measured, since the SQLite docs are ambiguous about it: deleting a
document *does* fire ``chunks_after_delete`` for the cascade-deleted chunks
without ``recursive_triggers``, so vectors and FTS rows are reaped correctly.)

Build a throwaway database and inspect it with ``python -m app.migrations``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Embedding width, and therefore the width of the vec0 column. The schema is the
# binding constraint here — a vec0 table's dimension is fixed at creation — so
# this is the single source of truth and app/embed/embed.py imports it.
# Changing it invalidates every stored vector: it needs a migration that drops
# and recreates chunk_vectors, plus a re-embed of every chunk.
EMBED_DIM = 1024

# ISO-8601 UTC, e.g. 2026-08-20T14:50:12.345Z. Sortable as text, readable in a DB
# browser, and the same string the frontend already receives today.
#
# Public because it is not only a column default: queries that used Postgres'
# NOW() need the identical expression, or a row written by a DEFAULT and a row
# written by a query would not compare correctly. Fixed-width and always UTC, so
# plain text comparison is chronological — which is what lets the stale-message
# sweep say `created_at < <NOW - 600 seconds>`.
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


# ---------------------------------------------------------------------------
# 1 — initial schema (the SQLite port of alembic 0001+0002+0003, folded flat)
# ---------------------------------------------------------------------------
_V1 = (
    f"""
    CREATE TABLE users (
        id              TEXT PRIMARY KEY,            -- uuid4, generated in Python
        email           TEXT NOT NULL UNIQUE,
        hashed_password TEXT NOT NULL,
        settings        TEXT NOT NULL DEFAULT '{{}}',  -- json
        openrouter_key  TEXT,                        -- encrypted at rest
        created_at      TEXT NOT NULL DEFAULT ({NOW})
    )
    """,

    f"""
    CREATE TABLE projects (
        id          TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        description TEXT,
        created_at  TEXT NOT NULL DEFAULT ({NOW}),
        updated_at  TEXT NOT NULL DEFAULT ({NOW})
    )
    """,
    "CREATE INDEX idx_projects_user_id ON projects(user_id)",

    # status is json so ingestion state stays flexible:
    #   {"state": "parsing"} / {"state": "ready", "chunks": 18} / {"state": "error", ...}
    f"""
    CREATE TABLE documents (
        id           TEXT PRIMARY KEY,
        project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        storage_key  TEXT NOT NULL,
        content_type TEXT,
        size_bytes   INTEGER,
        status       TEXT NOT NULL DEFAULT '{{"state":"uploaded"}}',
        summary      TEXT,
        created_at   TEXT NOT NULL DEFAULT ({NOW})
    )
    """,
    "CREATE INDEX idx_documents_project_id ON documents(project_id)",

    # `seq` exists alongside the uuid on purpose. It is the rowid alias that
    # chunk_vectors and chunks_fts join against, and declaring it explicitly is
    # what makes it stable: for a table whose primary key is TEXT, VACUUM is free
    # to renumber the implicit rowid, which would silently scramble both indexes.
    # `path` was a Postgres ltree ('doc.ch2.backprop'); labels are already
    # slugified to [a-z0-9_], so the dot separator stays unambiguous and
    # descendant tests become prefix matches on text.
    f"""
    CREATE TABLE chunks (
        seq            INTEGER PRIMARY KEY,
        id             TEXT NOT NULL UNIQUE,
        document_id    TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        chunk_index    INTEGER NOT NULL,
        content_full   TEXT NOT NULL,
        content_sparse TEXT,
        path           TEXT,
        umap_2d        TEXT,                          -- json {{x,y}}, null until computed
        umap_3d        TEXT,                          -- json {{x,y,z}}
        pca_coords     TEXT,
        tsne_coords    TEXT,
        study_status   TEXT NOT NULL DEFAULT '{{}}',
        metadata       TEXT NOT NULL DEFAULT '{{}}',
        created_at     TEXT NOT NULL DEFAULT ({NOW}),
        UNIQUE (document_id, chunk_index)
    )
    """,
    "CREATE INDEX idx_chunks_document_id ON chunks(document_id)",
    "CREATE INDEX idx_chunks_path        ON chunks(path)",

    # Dense vectors live in their own virtual table (sqlite-vec). Brute-force
    # cosine over every row — no ANN index — which measures ~210ms at 50k x 1024
    # on disk, well inside what this app needs.
    f"""
    CREATE VIRTUAL TABLE chunk_vectors USING vec0(
        chunk_seq INTEGER PRIMARY KEY,
        embedding float[{EMBED_DIM}] distance_metric=cosine
    )
    """,

    # Keyword search (the projection map's "bm25" mode). external-content FTS5:
    # the index points back at chunks rather than storing a second copy of the
    # text. 'porter' gives the stemming Postgres' 'english' config was doing.
    """
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
        content_full,
        content='chunks',
        content_rowid='seq',
        tokenize='porter unicode61'
    )
    """,

    # External-content FTS5 is not maintained automatically; these three triggers
    # are the contract. The delete trigger also reaps the row's vector, because
    # foreign-key cascades do not reach virtual tables.
    """
    CREATE TRIGGER chunks_after_insert AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, content_full) VALUES (new.seq, new.content_full);
    END
    """,
    """
    CREATE TRIGGER chunks_after_delete AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, content_full)
            VALUES ('delete', old.seq, old.content_full);
        DELETE FROM chunk_vectors WHERE chunk_seq = old.seq;
    END
    """,
    """
    CREATE TRIGGER chunks_after_update AFTER UPDATE OF content_full ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, content_full)
            VALUES ('delete', old.seq, old.content_full);
        INSERT INTO chunks_fts(rowid, content_full) VALUES (new.seq, new.content_full);
    END
    """,

    # Nested chat threading. `path` was an ltree; labels are zero-padded decimals
    # ('0001.0002.0001') because both ltree and text compare labels
    # lexicographically, so an unpadded '10' sorts before '2' and scrambles
    # `ORDER BY path` once a level has ten or more siblings.
    f"""
    CREATE TABLE messages (
        id         TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        chat_id    TEXT NOT NULL,
        path       TEXT NOT NULL,
        role       TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
        content    TEXT NOT NULL,
        metadata   TEXT NOT NULL DEFAULT '{{}}',
        created_at TEXT NOT NULL DEFAULT ({NOW})
    )
    """,
    "CREATE INDEX idx_messages_project_id ON messages(project_id)",
    # Serves the chat filter and the ORDER BY path in one index.
    "CREATE INDEX idx_messages_chat_path  ON messages(chat_id, path)",

    # centroid is a raw float32 blob, not a vec0 row: nothing searches it by
    # similarity, the medoid strategy retrieves through medoid_chunk_id.
    f"""
    CREATE TABLE clusters (
        id              TEXT PRIMARY KEY,
        project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        parent_id       TEXT REFERENCES clusters(id) ON DELETE CASCADE,
        level           INTEGER NOT NULL,
        label           TEXT,
        centroid        BLOB,
        medoid_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
        member_count    INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT ({NOW})
    )
    """,
    "CREATE INDEX idx_clusters_project_id ON clusters(project_id)",
    "CREATE INDEX idx_clusters_parent_id  ON clusters(parent_id)",

    f"""
    CREATE TABLE artifacts (
        id          TEXT PRIMARY KEY,
        project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        type        TEXT NOT NULL,
        storage_key TEXT,
        content     TEXT,                             -- json
        metadata    TEXT NOT NULL DEFAULT '{{}}',
        created_at  TEXT NOT NULL DEFAULT ({NOW})
    )
    """,
    "CREATE INDEX idx_artifacts_project_id ON artifacts(project_id)",
    "CREATE INDEX idx_artifacts_type       ON artifacts(type)",

    # The job queue that replaces Redis/ARQ (Phase 2). `run_after` carries what
    # ARQ's _defer_by did — finalize_project re-queues itself into the future
    # while it waits for a batch of ingests to finish.
    f"""
    CREATE TABLE jobs (
        id         INTEGER PRIMARY KEY,
        func       TEXT NOT NULL,
        args       TEXT NOT NULL DEFAULT '[]',        -- json array
        state      TEXT NOT NULL DEFAULT 'queued'
                   CHECK (state IN ('queued','running','done','error')),
        run_after  TEXT NOT NULL DEFAULT ({NOW}),
        attempts   INTEGER NOT NULL DEFAULT 0,
        error      TEXT,
        created_at TEXT NOT NULL DEFAULT ({NOW}),
        updated_at TEXT NOT NULL DEFAULT ({NOW})
    )
    """,
    # The claim query is WHERE state='queued' AND run_after<=now ORDER BY id.
    "CREATE INDEX idx_jobs_claim ON jobs(state, run_after)",
)


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, _V1),
)

SCHEMA_VERSION = MIGRATIONS[-1][0]


async def current_version(db) -> int:
    """The database's schema version (0 for a fresh file)."""
    row = await (await db.execute("PRAGMA user_version")).fetchone()
    return int(row[0])


async def migrate(db) -> int:
    """Apply every migration the database hasn't seen. Returns the new version.

    Each migration runs in its own transaction, with the ``user_version`` bump
    inside it — so a failure mid-migration rolls back the whole step and the
    version stays where it was, rather than leaving a half-applied schema that
    the next startup would skip.
    """
    version = await current_version(db)
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema is version {version}, but this build only knows "
            f"{SCHEMA_VERSION} — it was written by a newer version of F7ows"
        )
    if version == SCHEMA_VERSION:
        return version

    if db.isolation_level is not None:
        # Do not "fix" this by assigning db.isolation_level here: aiosqlite's
        # setter deadlocks its own connection thread. It has to be passed to
        # aiosqlite.connect().
        raise RuntimeError(
            "migrate() needs a connection opened with isolation_level=None "
            "(autocommit), so BEGIN/COMMIT below are ours to control"
        )

    for target, statements in MIGRATIONS:
        if target <= version:
            continue
        logger.info("migrations: applying %d -> %d", version, target)
        await db.execute("BEGIN")
        try:
            for statement in statements:
                await db.execute(statement)
            # PRAGMA can't be parameterised; target is our own int literal.
            await db.execute(f"PRAGMA user_version = {target}")
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            logger.exception("migrations: %d -> %d failed, rolled back", version, target)
            raise
        version = target

    logger.info("migrations: at version %d", version)
    return version


# ---------------------------------------------------------------------------
# Harness:  python -m app.migrations [path]
#
# Builds a database from scratch at `path` (default: a throwaway temp file),
# prints the resulting schema, and re-runs migrate() to prove it's a no-op the
# second time. Deliberately standalone — it does not import app.db, so it works
# before any of the app is wired up and needs no .env.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio
    import pathlib
    import sys
    import tempfile

    async def _main() -> None:
        import aiosqlite
        import sqlite_vec

        if len(sys.argv) > 1:
            path = pathlib.Path(sys.argv[1])
        else:
            path = pathlib.Path(tempfile.gettempdir()) / "f7ows_schema_check.db"
        for suffix in ("", "-wal", "-shm"):
            candidate = pathlib.Path(str(path) + suffix)
            if candidate.exists():
                candidate.unlink()
        print(f"building {path}\n")

        db = await aiosqlite.connect(path, isolation_level=None)
        await db.enable_load_extension(True)
        await db._execute(sqlite_vec.load, db._conn)
        await db.enable_load_extension(False)
        for pragma in ("journal_mode=WAL", "foreign_keys=ON", "busy_timeout=5000"):
            await db.execute(f"PRAGMA {pragma}")

        version = await migrate(db)
        print(f"migrated to version {version}\n")

        rows = await (await db.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'chunks_fts_%' ORDER BY type, name"
        )).fetchall()
        for kind, name in rows:
            print(f"  {kind:14} {name}")

        again = await migrate(db)
        print(f"\nre-running migrate(): version {again} (no-op)")
        await db.close()

    asyncio.run(_main())
