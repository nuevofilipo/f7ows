"""
ARQ worker — background job definitions and WorkerSettings.

Run with:
    arq app.worker.WorkerSettings
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from uuid import UUID

from arq.connections import RedisSettings

from app.chunk.chunk import chunk as chunk_markdown
from app.config import settings
from app.db import acquire, create_pool, close_pool
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
    """Write the document's ingestion state (JSONB) — the UI polls this."""
    async with acquire() as conn:
        await conn.execute(
            "UPDATE documents SET status = $2::jsonb WHERE id = $1",
            document_id, json.dumps({"state": state, **extra}),
        )


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

async def ingest_document(ctx: dict, document_id: str) -> dict:
    """
    Full ingestion pipeline for a single document (PHASE4_PLAN_v2 §1):
    download PDF -> parse -> chunk -> embed -> insert chunks -> mark ready.

    parse() and chunk() are CPU-bound and synchronous, so they run in a
    threadpool to avoid blocking the worker's event loop. On any failure the
    document is marked "error" with the message and the exception re-raised so
    ARQ records the job as failed.
    """
    doc_id = UUID(str(document_id))
    logger.info("ingest_document: %s", doc_id)
    try:
        async with acquire() as conn:
            doc = await conn.fetchrow(
                "SELECT name, storage_key, project_id FROM documents WHERE id = $1", doc_id
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
            with tr.timer("embed"):
                embeddings = await embed([c.content_full for c in chunks])
            tr.write("02_embedded.md", _render_embeddings(chunks, embeddings))

            rows = [
                (
                    doc_id, c.chunk_index, c.content_full, c.content_sparse, emb, c.path,
                    json.dumps({"heading_trail": c.heading_trail, "token_count": c.token_count}),
                )
                for c, emb in zip(chunks, embeddings)
            ]
            async with acquire() as conn:
                async with conn.transaction():
                    # Idempotent re-ingest: clear any prior chunks for this document.
                    await conn.execute("DELETE FROM chunks WHERE document_id = $1", doc_id)
                    await conn.executemany(
                        """
                        INSERT INTO chunks
                            (document_id, chunk_index, content_full, content_sparse,
                             embedding, path, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6::ltree, $7::jsonb)
                        """,
                        rows,
                    )

            await _set_status(doc_id, "ready", chunks=len(chunks))
            # NOTE: projections/clusters are intentionally NOT enqueued here. A new
            # document shifts the whole project map, so recomputing per-document would
            # redo that work N times for an N-file batch. Instead the upload endpoint
            # enqueues a single finalize_project job that waits for all the batch's
            # documents and recomputes once.
            logger.info("ingest_document: %s ready (%d chunks)", doc_id, len(chunks))
            return {"status": "ok", "document_id": str(doc_id), "chunks": len(chunks)}

    except Exception as exc:  # noqa: BLE001 - record on the doc, then re-raise for ARQ
        logger.exception("ingest_document failed: %s", doc_id)
        await _set_status(doc_id, "error", error=str(exc))
        raise


async def generate_apkg(ctx: dict, artifact_id: str) -> dict:
    """
    Generate an Anki .apkg file from a set of flashcards stored in Postgres,
    upload to R2, update the artifact row with the storage key.
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


async def recalculate_projections(ctx: dict, project_id: str) -> dict:
    """
    Recompute display coordinates for an entire project: pull every chunk's
    embedding, reduce the whole matrix (UMAP 2D+3D, PCA, t-SNE), and write the
    coordinates back per chunk. Project-wide on purpose — a new document moves
    every point, so the whole map is recomputed (PHASE4_PLAN_v2 §1 / PRD).

    CPU-heavy; reduction runs in a threadpool. Coordinates land in the existing
    JSONB columns and are null until this completes (the UI treats null as
    "not plotted yet").
    """
    import numpy as np

    from app.projections import compute

    pid = UUID(str(project_id))
    logger.info("recalculate_projections: %s", pid)

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.embedding
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.project_id = $1 AND c.embedding IS NOT NULL
            ORDER BY c.id
            """,
            pid,
        )
    if not rows:
        return {"status": "ok", "project_id": str(pid), "chunks": 0}

    ids = [r["id"] for r in rows]
    vectors = np.array([r["embedding"].to_numpy() for r in rows], dtype=np.float32)
    coords = await asyncio.to_thread(compute, vectors)

    updates = [
        (
            cid,
            _coord_json(coords["umap_2d"], i, ("x", "y")),
            _coord_json(coords["umap_3d"], i, ("x", "y", "z")),
            _coord_json(coords["pca"], i, ("x", "y")),
            _coord_json(coords["tsne"], i, ("x", "y")),
        )
        for i, cid in enumerate(ids)
    ]
    async with acquire() as conn:
        await conn.executemany(
            """
            UPDATE chunks
            SET umap_2d = $2::jsonb, umap_3d = $3::jsonb,
                pca_coords = $4::jsonb, tsne_coords = $5::jsonb
            WHERE id = $1
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


async def recalculate_clusters(ctx: dict, project_id: str) -> dict:
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
            SELECT c.id, c.embedding, c.content_sparse
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.project_id = $1 AND c.embedding IS NOT NULL
            ORDER BY c.id
            """,
            pid,
        )
    if not rows:
        return {"status": "ok", "project_id": str(pid), "clusters": 0}

    ids = [r["id"] for r in rows]
    vectors = np.array([r["embedding"].to_numpy() for r in rows], dtype=np.float32)
    sparse = [r["content_sparse"] or "" for r in rows]
    results = await asyncio.to_thread(compute_clusters, vectors, sparse)

    async with acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM clusters WHERE project_id = $1", pid)
            await conn.executemany(
                """
                INSERT INTO clusters
                    (project_id, parent_id, level, label, centroid, medoid_chunk_id, member_count)
                VALUES ($1, NULL, 0, $2, $3, $4, $5)
                """,
                [(pid, r.label, r.centroid, ids[r.medoid_index], r.member_count) for r in results],
            )

    md = [f"# Clusters ({len(results)})\n",
          "| members | label | medoid chunk |", "|--------:|-------|--------------|"]
    for r in sorted(results, key=lambda r: r.member_count, reverse=True):
        md.append(f"| {r.member_count} | {r.label!r} | `{ids[r.medoid_index]}` |")
    trace.project_artifact(pid, "clusters.md", "\n".join(md))
    logger.info("recalculate_clusters: %s wrote %d clusters", pid, len(results))
    return {"status": "ok", "project_id": str(pid), "clusters": len(results)}


# Coalescing recompute: how long/often finalize_project waits for ingests.
# but what do you mean it only waits that long, doesn't it need to wait forever, because at any point the job might arrive?
_FINALIZE_RETRY = timedelta(seconds=5)
_FINALIZE_MAX_ATTEMPTS = 120  # ~10 min ceiling before recomputing anyway


async def finalize_project(
    ctx: dict, project_id: str, document_ids: list[str],
    attempts_left: int = _FINALIZE_MAX_ATTEMPTS,
) -> dict:
    """
    Recompute a project's projections + clusters ONCE, after a batch of uploads.

    Projections and clustering are project-wide, so doing them per document would
    repeat the work for every file in a batch. The upload endpoint enqueues a
    single finalize_project for the batch; it waits until all of ``document_ids``
    reach a terminal state ("ready"/"error") and only then enqueues the two
    recompute jobs. Waiting is cooperative — if documents are still ingesting it
    re-enqueues itself deferred (freeing the worker slot) rather than sleeping.
    """
    pid = UUID(str(project_id))
    ids = [UUID(str(d)) for d in document_ids]
    async with acquire() as conn:
        pending = await conn.fetchval(
            """
            SELECT COUNT(*) FROM documents
            WHERE id = ANY($1::uuid[])
              AND COALESCE(status->>'state', '') NOT IN ('ready', 'error')
            """,
            ids,
        )

    redis = ctx.get("redis")
    if pending and attempts_left > 0:
        if redis is not None:
            await redis.enqueue_job(
                "finalize_project", str(pid), [str(i) for i in ids], attempts_left - 1,
                _defer_by=_FINALIZE_RETRY,
            )
        return {"status": "waiting", "project_id": str(pid), "pending": int(pending)}

    if redis is not None:
        await redis.enqueue_job("recalculate_projections", str(pid))
        await redis.enqueue_job("recalculate_clusters", str(pid))
    logger.info("finalize_project: %s recompute enqueued (%d docs)", pid, len(ids))
    return {"status": "finalized", "project_id": str(pid), "documents": len(ids)}


# ---------------------------------------------------------------------------
# Worker lifecycle hooks
# ---------------------------------------------------------------------------

async def startup(ctx: dict) -> None:
    ctx["pool"] = await create_pool()
    logger.info("ARQ worker: database pool created")


async def shutdown(ctx: dict) -> None:
    await close_pool()
    logger.info("ARQ worker: database pool closed")


# ---------------------------------------------------------------------------
# WorkerSettings — referenced by `arq app.worker.WorkerSettings`
# ---------------------------------------------------------------------------

class WorkerSettings:
    functions = [ingest_document, generate_apkg, recalculate_projections,
                 recalculate_clusters, finalize_project]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_public_url)
    max_jobs = 10
    job_timeout = 600  # 10 min max per job


# ---------------------------------------------------------------------------
# Manual test harness:  python -m app.worker [document_id]
#
# Runs ingest_document directly against the live DB + bucket — no Redis/ARQ
# needed (ARQ would just call the same function). With no argument it picks the
# most recently uploaded document. Prints the resulting status and a few chunks.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    async def _main() -> None:
        await create_pool()
        try:
            async with acquire() as conn:
                if len(sys.argv) > 1:
                    doc_id = UUID(sys.argv[1])
                else:
                    doc_id = await conn.fetchval(
                        "SELECT id FROM documents ORDER BY created_at DESC LIMIT 1"
                    )
                    if doc_id is None:
                        raise SystemExit("No documents in the DB — upload one first.")
                    print(f"Ingesting most recent document: {doc_id}")

            result = await ingest_document({}, str(doc_id))
            print("\nresult:", result)

            async with acquire() as conn:
                status = await conn.fetchval("SELECT status FROM documents WHERE id = $1", doc_id)
                rows = await conn.fetch(
                    """
                    SELECT chunk_index, path, token_count_meta, LENGTH(content_full) AS full_len,
                           LENGTH(content_sparse) AS sparse_len, (embedding IS NOT NULL) AS has_emb
                    FROM (
                        SELECT chunk_index, path, content_full, content_sparse, embedding,
                               (metadata->>'token_count')::int AS token_count_meta
                        FROM chunks WHERE document_id = $1 ORDER BY chunk_index
                    ) s
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
                    "SELECT project_id FROM documents WHERE id = $1", doc_id
                )
            proj_result = await recalculate_projections({}, str(project_id))
            print("\nprojections:", proj_result)
            async with acquire() as conn:
                sample = await conn.fetch(
                    """
                    SELECT umap_2d, umap_3d, pca_coords, tsne_coords
                    FROM chunks c JOIN documents d ON d.id = c.document_id
                    WHERE d.project_id = $1 LIMIT 3
                    """,
                    project_id,
                )
            for s in sample:
                print(f"  umap2d={s['umap_2d']} umap3d={s['umap_3d']} "
                      f"pca={s['pca_coords']} tsne={s['tsne_coords']}")

            # Step 8: cluster the project and show the resulting nodes.
            clu_result = await recalculate_clusters({}, str(project_id))
            print("\nclusters:", clu_result)
            async with acquire() as conn:
                cl = await conn.fetch(
                    """
                    SELECT cl.label, cl.member_count, ch.path AS medoid_path
                    FROM clusters cl LEFT JOIN chunks ch ON ch.id = cl.medoid_chunk_id
                    WHERE cl.project_id = $1 ORDER BY cl.member_count DESC
                    """,
                    project_id,
                )
            for c in cl:
                print(f"  [{c['member_count']:>2}] {c['label']!r}  medoid={c['medoid_path']}")
        finally:
            await close_pool()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_main())
