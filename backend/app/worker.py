"""
Background worker — the job functions, and the loop that runs them.

Run with:
    python -m app.worker

The queue is the `jobs` table (app/migrations.py), not Redis; app/producer.py is
the enqueueing half. This process claims one job at a time, runs it, and marks it
done or failed.

Two processes, deliberately, exactly as before: the web app must not run these.
Parsing a PDF and fitting UMAP are second-to-minute CPU work, and inside the API
process they would stall every request — including the status polls the UI uses
to render ingest progress.
"""
from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID, uuid4

from app import producer
from app.chunk.chunk import chunk as chunk_markdown
from app.db import NOW, acquire, close_pool, create_pool, placeholders, serialize_vector
from app.embed.embed import embed
from app.ingest.parse import parse
from app.library import storage
from app import trace

logger = logging.getLogger(__name__)


def _render_chunks(chunks) -> str:
    """Per-chunk table + full-vs-sparse content, so chunking quality is eyeballable."""
    lines = [f"# Chunks ({len(chunks)})\n",
             "| idx | tokens | path | heading trail |",
             "|----:|-------:|------|---------------|"]
    for c in chunks:
        trail = " › ".join(c.heading_trail) if c.heading_trail else ""
        lines.append(f"| {c.chunk_index} | {c.token_count} | `{c.path}` | {trail} |")
    lines.append("\n---")
    for c in chunks:
        lines.append(f"\n## [{c.chunk_index}] `{c.path}`  ({c.token_count} tok)\n")
        lines.append("**full:**\n\n```\n" + c.content_full + "\n```\n")
        lines.append("**sparse:**\n\n```\n" + c.content_sparse + "\n```")
    return "\n".join(lines)


def _render_embeddings(chunks, embeddings) -> str:
    dim = len(embeddings[0]) if embeddings else 0
    lines = ["# Embeddings\n", f"- vectors: {len(embeddings)}", f"- dim: {dim}\n"]
    for c, e in zip(chunks, embeddings):
        head = ", ".join(f"{float(x):+.3f}" for x in list(e)[:6])
        lines.append(f"- [{c.chunk_index}] `{c.path}` → [{head}, …]")
    return "\n".join(lines)


async def _set_status(document_id: UUID, state: str, **extra) -> None:
    """Write the document's ingestion state (JSON) — the UI polls this."""
    async with acquire() as conn:
        await conn.execute(
            "UPDATE documents SET status = ? WHERE id = ?",
            json.dumps({"state": state, **extra}), document_id,
        )


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

async def ingest_document(document_id: str) -> dict:
    """
    Full ingestion pipeline for a single document (PHASE4_PLAN_v2 §1):
    download PDF -> parse -> chunk -> embed -> insert chunks -> mark ready.

    parse() and chunk() are CPU-bound and synchronous, so they run in a
    threadpool to avoid blocking the worker's event loop. On any failure the
    document is marked "error" with the message and the exception re-raised so
    the loop records the job as failed.
    """
    doc_id = UUID(str(document_id))
    logger.info("ingest_document: %s", doc_id)
    try:
        async with acquire() as conn:
            doc = await conn.fetchrow(
                """
                SELECT d.name, d.storage_key, d.project_id
                FROM documents d
                WHERE d.id = ?
                """,
                doc_id,
            )
        if doc is None:
            raise ValueError(f"document {doc_id} not found")

        with trace.begin_run("ingest", label=doc["name"],
                             document_id=str(doc_id), project_id=str(doc["project_id"])) as tr:
            await _set_status(doc_id, "parsing")
            pdf_bytes = await storage.download_bytes(doc["storage_key"])
            with tr.timer("parse"):
                markdown = await asyncio.to_thread(parse, pdf_bytes, name=doc["name"])
            tr.write("00_parsed.md", markdown)

            doc_slug = doc["name"].rsplit(".", 1)[0] or str(doc_id)
            with tr.timer("chunk"):
                chunks = await asyncio.to_thread(chunk_markdown, markdown, doc_slug=doc_slug)
            if not chunks:
                tr.set(chunks=0)
                await _set_status(doc_id, "ready", chunks=0)
                return {"status": "ok", "document_id": str(doc_id), "chunks": 0}
            tr.write("01_chunked.md", _render_chunks(chunks))
            tr.set(chunks=len(chunks))

            await _set_status(doc_id, "embedding", chunks=len(chunks))
            # The endpoint and key come from the embed role's provider at call
            # time (app/providers.py); nothing is threaded through the job.
            with tr.timer("embed"):
                embeddings = await embed([c.content_full for c in chunks])
            tr.write("02_embedded.md", _render_embeddings(chunks, embeddings))

            async with acquire() as conn:
                async with conn.transaction():
                    # Idempotent re-ingest: clear any prior chunks for this
                    # document. The chunks_after_delete trigger reaps their
                    # vectors and FTS rows, which no foreign key would reach:
                    # chunk_vectors and chunks_fts are virtual tables.
                    await conn.execute("DELETE FROM chunks WHERE document_id = ?", doc_id)
                    # One row at a time rather than executemany: the vector lives
                    # in a separate table keyed by the chunk's integer `seq`, and
                    # the only way to learn a freshly inserted seq is lastrowid on
                    # that statement's own cursor. Still one transaction, still
                    # in-process, so the extra round trips cost nothing measurable.
                    for c, emb in zip(chunks, embeddings):
                        cursor = await conn.execute(
                            """
                            INSERT INTO chunks
                                (id, document_id, chunk_index, content_full,
                                 content_sparse, path, metadata)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            uuid4(), doc_id, c.chunk_index, c.content_full,
                            c.content_sparse, c.path,
                            json.dumps({"heading_trail": c.heading_trail,
                                        "token_count": c.token_count}),
                        )
                        await conn.execute(
                            "INSERT INTO chunk_vectors (chunk_seq, embedding) VALUES (?, ?)",
                            cursor.lastrowid, serialize_vector(emb),
                        )

            await _set_status(doc_id, "ready", chunks=len(chunks))
            # NOTE: projections/clusters are intentionally NOT enqueued here. A new
            # document shifts the whole project map, so recomputing per-document would
            # redo that work N times for an N-file batch. Instead the upload endpoint
            # enqueues a single finalize_project job that waits for all the batch's
            # documents and recomputes once.
            logger.info("ingest_document: %s ready (%d chunks)", doc_id, len(chunks))
            return {"status": "ok", "document_id": str(doc_id), "chunks": len(chunks)}

    except Exception as exc:  # noqa: BLE001 - record on the doc, then re-raise for the loop
        logger.exception("ingest_document failed: %s", doc_id)
        await _set_status(doc_id, "error", error=str(exc))
        raise


async def generate_apkg(artifact_id: str) -> dict:
    """
    Generate an Anki .apkg file from a set of flashcards stored in the database,
    write it to local storage, update the artifact row with the storage key.
    """
    logger.info("generate_apkg: artifact_id=%s", artifact_id)
    # TODO: implement
    return {"status": "ok", "artifact_id": artifact_id}


def _coord_json(arr, i: int, keys: tuple[str, ...]) -> str | None:
    """One chunk's row of a reduction as a JSON object, or None if the method
    didn't run (-> SQL NULL)."""
    if arr is None:
        return None
    return json.dumps({k: float(arr[i][j]) for j, k in enumerate(keys)})


async def recalculate_projections(project_id: str) -> dict:
    """
    Recompute display coordinates for an entire project: pull every chunk's
    embedding, reduce the whole matrix (UMAP 2D+3D, PCA, t-SNE), and write the
    coordinates back per chunk. Project-wide on purpose — a new document moves
    every point, so the whole map is recomputed (PHASE4_PLAN_v2 §1 / PRD).

    CPU-heavy; reduction runs in a threadpool. Coordinates land in the existing
    JSON columns and are null until this completes (the UI treats null as
    "not plotted yet").
    """
    import numpy as np

    from app.projections import compute

    pid = UUID(str(project_id))
    logger.info("recalculate_projections: %s", pid)

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, v.embedding
            FROM chunks c
            JOIN chunk_vectors v ON v.chunk_seq = c.seq
            JOIN documents d ON d.id = c.document_id
            WHERE d.project_id = ?
            ORDER BY c.id
            """,
            pid,
        )
    if not rows:
        return {"status": "ok", "project_id": str(pid), "chunks": 0}

    ids = [r["id"] for r in rows]
    # sqlite-vec hands the column back as the raw little-endian float32 blob it
    # stores, so numpy reads it directly (pgvector returned its own type, with a
    # .to_numpy() to get here).
    vectors = np.array([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows],
                       dtype=np.float32)
    coords = await asyncio.to_thread(compute, vectors)

    # id last: placeholders are positional now, so each tuple has to follow the
    # statement's SET-then-WHERE order rather than asyncpg's $1-first.
    updates = [
        (
            _coord_json(coords["umap_2d"], i, ("x", "y")),
            _coord_json(coords["umap_3d"], i, ("x", "y", "z")),
            _coord_json(coords["pca"], i, ("x", "y")),
            _coord_json(coords["tsne"], i, ("x", "y")),
            cid,
        )
        for i, cid in enumerate(ids)
    ]
    async with acquire() as conn:
        await conn.executemany(
            """
            UPDATE chunks
            SET umap_2d = ?, umap_3d = ?, pca_coords = ?, tsne_coords = ?
            WHERE id = ?
            """,
            updates,
        )

    methods = {m: (coords[m].shape if coords[m] is not None else None)
               for m in ("umap_2d", "umap_3d", "pca", "tsne")}
    trace.project_artifact(pid, "projections.md",
                           f"# Projections\n\n- chunks: {len(ids)}\n" +
                           "\n".join(f"- **{m}**: {shape}" for m, shape in methods.items()))
    logger.info("recalculate_projections: %s wrote coords for %d chunks", pid, len(ids))
    return {"status": "ok", "project_id": str(pid), "chunks": len(ids)}


async def recalculate_clusters(project_id: str) -> dict:
    """
    Recompute the project's semantic clusters (single level for now): pull every
    chunk's embedding + sparse text, run HDBSCAN, and replace the project's rows
    in `clusters` with one row per cluster (centroid, medoid chunk, TF-IDF label).
    Used by the global-query router and the map UI. CPU-heavy -> threadpool.
    """
    import numpy as np

    from app.clusters import compute_clusters

    pid = UUID(str(project_id))
    logger.info("recalculate_clusters: %s", pid)

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, v.embedding, c.content_sparse
            FROM chunks c
            JOIN chunk_vectors v ON v.chunk_seq = c.seq
            JOIN documents d ON d.id = c.document_id
            WHERE d.project_id = ?
            ORDER BY c.id
            """,
            pid,
        )
    if not rows:
        return {"status": "ok", "project_id": str(pid), "clusters": 0}

    ids = [r["id"] for r in rows]
    vectors = np.array([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows],
                       dtype=np.float32)
    sparse = [r["content_sparse"] or "" for r in rows]
    results = await asyncio.to_thread(compute_clusters, vectors, sparse)

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM clusters WHERE project_id = ?", pid)
            await conn.executemany(
                """
                INSERT INTO clusters
                    (id, project_id, parent_id, level, label, centroid,
                     medoid_chunk_id, member_count)
                VALUES (?, ?, NULL, 0, ?, ?, ?, ?)
                """,
                # centroid is stored as a plain float32 blob, not a vec0 row:
                # nothing searches clusters by similarity (the medoid strategy
                # retrieves through medoid_chunk_id), so it needs no index. Same
                # packing as a vector, hence the same helper.
                [(uuid4(), pid, r.label, serialize_vector(r.centroid),
                  ids[r.medoid_index], r.member_count)
                 for r in results],
            )

    md = [f"# Clusters ({len(results)})\n",
          "| members | label | medoid chunk |", "|--------:|-------|--------------|"]
    for r in sorted(results, key=lambda r: r.member_count, reverse=True):
        md.append(f"| {r.member_count} | {r.label!r} | `{ids[r.medoid_index]}` |")
    trace.project_artifact(pid, "clusters.md", "\n".join(md))
    logger.info("recalculate_clusters: %s wrote %d clusters", pid, len(results))
    return {"status": "ok", "project_id": str(pid), "clusters": len(results)}


# Coalescing recompute: how long/often finalize_project waits for ingests.
# The ceiling is not "give up on the batch" — it is "stop waiting and recompute
# anyway". A document that never reaches a terminal state (worker killed
# mid-parse, say) would otherwise hold the project's map hostage forever, so
# after ~10 minutes we project and cluster whatever did land. The next upload
# enqueues another finalize and the map catches up.
_FINALIZE_RETRY_SECONDS = 5
_FINALIZE_MAX_ATTEMPTS = 120  # ~10 min ceiling before recomputing anyway


async def finalize_project(
    project_id: str, document_ids: list[str],
    attempts_left: int = _FINALIZE_MAX_ATTEMPTS,
) -> dict:
    """
    Recompute a project's projections + clusters ONCE, after a batch of uploads.

    Projections and clustering are project-wide, so doing them per document would
    repeat the work for every file in a batch. The upload endpoint enqueues a
    single finalize_project for the batch; it waits until all of ``document_ids``
    reach a terminal state ("ready"/"error") and only then enqueues the two
    recompute jobs. Waiting is cooperative — if documents are still ingesting it
    re-enqueues itself with a ``run_after`` a few seconds out (freeing the worker)
    rather than sleeping, which is what ARQ's ``_defer_by`` used to do.
    """
    pid = UUID(str(project_id))
    ids = [UUID(str(d)) for d in document_ids]
    async with acquire() as conn:
        pending = await conn.fetchval(
            f"""
            SELECT COUNT(*) FROM documents
            WHERE id IN ({placeholders(ids)})
              AND COALESCE(json_extract(status, '$.state'), '') NOT IN ('ready', 'error')
            """,
            *ids,
        )

    if pending and attempts_left > 0:
        await producer.enqueue_finalize(
            pid, ids, attempts_left=attempts_left - 1,
            delay_seconds=_FINALIZE_RETRY_SECONDS,
        )
        return {"status": "waiting", "project_id": str(pid), "pending": int(pending)}

    await producer.enqueue_projections(pid)
    await producer.enqueue_clusters(pid)
    logger.info("finalize_project: %s recompute enqueued (%d docs)", pid, len(ids))
    return {"status": "finalized", "project_id": str(pid), "documents": len(ids)}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

# The dispatch table. A job row stores its function by name, so this is also the
# allowlist: a name that isn't here fails the job instead of resolving to some
# arbitrary attribute of this module.
JOBS = {
    "ingest_document": ingest_document,
    "generate_apkg": generate_apkg,
    "recalculate_projections": recalculate_projections,
    "recalculate_clusters": recalculate_clusters,
    "finalize_project": finalize_project,
}

# How long to wait before looking again when the queue is empty. Redis could
# block until a job arrived; polling is the price of not shipping Redis. A
# quarter second is imperceptible next to the upload it follows, and an idle
# SELECT against a warm index is far too cheap to matter on a laptop.
_IDLE_SLEEP_SECONDS = 0.25

# A failed job is retried, because the most likely failure here is a network blip
# mid-embed rather than a bad PDF. Deterministic failures still stop quickly.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 10


async def _claim() -> dict | None:
    """Take the next eligible job, or None if there is nothing to do.

    ``attempt`` is the count *including* this claim — the SELECT reads the
    counter before the UPDATE bumps it, so returning the raw column would make
    the first run look like attempt 0 and buy one extra retry.

    The SELECT and the UPDATE are one transaction so a job cannot be claimed
    twice. That matters even though only one worker is expected to run: nothing
    stops a user from launching a second copy of the app, and BEGIN IMMEDIATE
    plus the `state = 'queued'` guard makes the loser of that race see zero
    updated rows and move on rather than run the job a second time.
    """
    async with acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"""
                SELECT id, func, args, attempts + 1 AS attempt FROM jobs
                WHERE state = 'queued' AND run_after <= {NOW}
                ORDER BY id
                LIMIT 1
                """
            )
            if row is None:
                return None
            cursor = await conn.execute(
                f"""
                UPDATE jobs SET state = 'running', attempts = attempts + 1,
                                updated_at = {NOW}
                WHERE id = ? AND state = 'queued'
                """,
                row["id"],
            )
            if cursor.rowcount != 1:
                return None       # someone else got there first
    return dict(row)


async def _finish(job_id: int, state: str, error: str | None = None,
                  delay_seconds: float = 0) -> None:
    """Record a job's outcome: done, error, or queued again for a retry."""
    run_after = NOW if not delay_seconds else (
        f"strftime('%Y-%m-%dT%H:%M:%fZ','now','+{float(delay_seconds)} seconds')")
    async with acquire() as conn:
        await conn.execute(
            f"""
            UPDATE jobs SET state = ?, error = ?, updated_at = {NOW},
                            run_after = {run_after}
            WHERE id = ?
            """,
            state, error, job_id,
        )


async def run_job(job: dict) -> None:
    """Run one claimed job and record what happened. Never raises: a job that
    blows up must not take the worker down with it."""
    func = JOBS.get(job["func"])
    if func is None:
        logger.error("worker: unknown job function %r (job %s)", job["func"], job["id"])
        await _finish(job["id"], "error", f"unknown job function: {job['func']}")
        return

    args = json.loads(job["args"])
    try:
        result = await func(*args)
    except Exception as exc:  # noqa: BLE001 - the whole point is to not die here
        logger.exception("worker: job %s (%s) failed", job["id"], job["func"])
        if job["attempt"] < _MAX_ATTEMPTS:
            logger.info("worker: retrying job %s in %ds (attempt %d/%d)",
                        job["id"], _RETRY_BACKOFF_SECONDS, job["attempt"], _MAX_ATTEMPTS)
            await _finish(job["id"], "queued", str(exc),
                          delay_seconds=_RETRY_BACKOFF_SECONDS)
        else:
            await _finish(job["id"], "error", str(exc))
        return
    await _finish(job["id"], "done")
    logger.info("worker: job %s (%s) done: %s", job["id"], job["func"], result)


async def requeue_stranded() -> int:
    """Return jobs left `running` by a previous process to the queue.

    A job is marked `running` before it executes and only moves off that state
    when it finishes. So a row still sitting in `running` at startup means the
    worker that owned it never got to write an outcome — it was killed. On a
    server that is rare; on a laptop it is routine (force-quit, battery death,
    an OS update reboot, sleep that the process doesn't survive).

    Nothing else will ever pick those rows up: `_claim` only looks at `queued`.
    Under ARQ this was handled for us — a job whose worker died lost its lock and
    became visible again. The `jobs` table has no lease, so recovery has to be
    explicit, and without it a force-quit mid-ingest strands the document at
    "parsing" in the UI permanently.

    Safe to be indiscriminate here because this runs at startup, before the loop
    claims anything, and only one worker is expected: any `running` row at this
    moment is definitionally abandoned. Attempts are not reset, so a job that
    reliably kills the process still exhausts its retries rather than looping
    forever.
    """
    async with acquire() as conn:
        cursor = await conn.execute(
            f"UPDATE jobs SET state = 'queued', updated_at = {NOW} WHERE state = 'running'")
    n = cursor.rowcount
    if n:
        logger.warning("worker: requeued %d job(s) stranded by a previous run", n)
    return n


async def run_worker() -> None:
    """Claim and run jobs until cancelled.

    Assumes the database is already open. Opening and closing it belongs to
    whoever owns the process (see ``_serve`` below) — if the loop did it, then
    cancelling the loop would close a connection its caller might still be
    using, which is exactly what the queue's own test harness does.
    """
    await requeue_stranded()
    logger.info("worker: started, polling the jobs table")
    while True:
        job = await _claim()
        if job is None:
            await asyncio.sleep(_IDLE_SLEEP_SECONDS)
            continue
        await run_job(job)


async def _serve() -> None:
    """The worker process: open the database, run the loop, close it on the way out."""
    from app.logging_setup import setup as setup_logging

    setup_logging("worker")     # its own file: two processes sharing one interleave
    await create_pool()
    try:
        await run_worker()
    except asyncio.CancelledError:
        logger.info("worker: stopping")
    finally:
        await close_pool()


# ---------------------------------------------------------------------------
# Entry points:
#
#   python -m app.worker                  run the worker loop (this is the app)
#   python -m app.worker ingest [doc_id]  run one ingest directly and dump the
#                                         result — no queue involved. With no id
#                                         it picks the most recently uploaded
#                                         document.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    async def _harness() -> None:
        await create_pool()
        try:
            async with acquire() as conn:
                if len(sys.argv) > 2:
                    doc_id = UUID(sys.argv[2])
                else:
                    doc_id = await conn.fetchval(
                        "SELECT id FROM documents ORDER BY created_at DESC LIMIT 1"
                    )
                    if doc_id is None:
                        raise SystemExit("No documents in the DB — upload one first.")
                    print(f"Ingesting most recent document: {doc_id}")

            result = await ingest_document(str(doc_id))
            print("\nresult:", result)

            async with acquire() as conn:
                status = await conn.fetchval("SELECT status FROM documents WHERE id = ?", doc_id)
                rows = await conn.fetch(
                    """
                    SELECT c.chunk_index, c.path,
                           json_extract(c.metadata, '$.token_count') AS token_count_meta,
                           LENGTH(c.content_full) AS full_len,
                           LENGTH(c.content_sparse) AS sparse_len,
                           (v.chunk_seq IS NOT NULL) AS has_emb
                    FROM chunks c
                    LEFT JOIN chunk_vectors v ON v.chunk_seq = c.seq
                    WHERE c.document_id = ?
                    ORDER BY c.chunk_index
                    """,
                    doc_id,
                )
            print("status:", status)
            print(f"{len(rows)} chunks inserted; first few:")
            for r in rows[:8]:
                print(f"  [{r['chunk_index']:>3}] {r['token_count_meta']:>4}t  emb={r['has_emb']}  "
                      f"full={r['full_len']} sparse={r['sparse_len']}  {r['path']}")

            # Step 7: run projections for this document's project and show coords.
            async with acquire() as conn:
                project_id = await conn.fetchval(
                    "SELECT project_id FROM documents WHERE id = ?", doc_id
                )
            proj_result = await recalculate_projections(str(project_id))
            print("\nprojections:", proj_result)
            async with acquire() as conn:
                sample = await conn.fetch(
                    """
                    SELECT umap_2d, umap_3d, pca_coords, tsne_coords
                    FROM chunks c JOIN documents d ON d.id = c.document_id
                    WHERE d.project_id = ? LIMIT 3
                    """,
                    project_id,
                )
            for s in sample:
                print(f"  umap2d={s['umap_2d']} umap3d={s['umap_3d']} "
                      f"pca={s['pca_coords']} tsne={s['tsne_coords']}")

            # Step 8: cluster the project and show the resulting nodes.
            clu_result = await recalculate_clusters(str(project_id))
            print("\nclusters:", clu_result)
            async with acquire() as conn:
                cl = await conn.fetch(
                    """
                    SELECT cl.label, cl.member_count, ch.path AS medoid_path
                    FROM clusters cl LEFT JOIN chunks ch ON ch.id = cl.medoid_chunk_id
                    WHERE cl.project_id = ? ORDER BY cl.member_count DESC
                    """,
                    project_id,
                )
            for c in cl:
                print(f"  [{c['member_count']:>2}] {c['label']!r}  medoid={c['medoid_path']}")
        finally:
            await close_pool()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] == "ingest":
        asyncio.run(_harness())
    else:
        try:
            asyncio.run(_serve())
        except KeyboardInterrupt:
            pass
