"""
Projection API — read-only data for the map UI (PRD "Projection Page").

The heavy lifting already happened in the worker: every chunk has pre-computed
2D display coordinates (`umap_2d` / `pca_coords` / `tsne_coords`, JSONB) written by
``recalculate_projections``. This router just *serves* them, plus a search that
highlights matching chunks on the map.

Two endpoints, both scoped + owner-checked under a project:

* ``GET /projects/{id}/projection`` — every chunk with coordinates, its study
  state, and enough metadata to label/size a point. Coordinates are null until
  the worker runs (a fresh project), so ``ready`` says whether anything is
  plottable yet.
* ``GET /projects/{id}/projection/search`` — rank chunks against a query by
  cosine similarity (pgvector over the dense embedding) or BM25 (Postgres
  full-text ``ts_rank``). Returns ids + scores; the frontend highlights those
  points on the already-plotted map.
"""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import get_current_user
from app.chat.study import clear_studied
from app.db import acquire
from app.embed import embed

router = APIRouter(prefix="/projects/{project_id}/projection", tags=["projection"])


async def _owned_project(project_id: UUID, user_id) -> None:
    """404 unless the project exists and belongs to the current user."""
    async with acquire() as conn:
        owner = await conn.fetchval(
            "SELECT 1 FROM projects WHERE id = $1 AND user_id = $2",
            project_id, user_id,
        )
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")


def _json(value):
    """asyncpg hands back JSONB as a string (no codec registered); parse it.
    A coordinate column is null until the worker has projected the project."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _study_state(study_status) -> str:
    """Collapse a chunk's ``study_status`` JSONB into one of the three legend
    buckets the map colours by. The states are written by ``app.chat.study`` when
    a chunk is cited; an un-cited chunk keeps the default ``{}`` -> "not_studied":

        {"state": "actively_studied"}   -> filled accent  (active recall: mock exam)
        {"state": "passively_studied"}  -> accent outline  (passive: summary/chat)
        anything else / {}              -> "not_studied"   (paper outline)
    """
    blob = _json(study_status) or {}
    state = str(blob.get("state", "")).lower()
    if state in ("actively_studied", "actively_recalled", "active"):
        return "actively_studied"
    if state in ("passively_studied", "summarised", "summarized", "passive"):
        return "passively_studied"
    return "not_studied"


@router.get("")
async def get_projection(
    project_id: UUID,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Every chunk in the project with its display coordinates + study state.

    ``ready`` is false when no chunk has been projected yet (fresh project, or
    the recompute hasn't finished) so the UI can show an empty-state instead of a
    blank canvas.
    """
    await _owned_project(project_id, current_user["id"])
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.path, c.metadata, c.study_status,
                   c.umap_2d, c.pca_coords, c.tsne_coords,
                   d.name AS document_name
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE d.project_id = $1
            ORDER BY c.id
            """,
            project_id,
        )

    chunks = []
    ready = False
    for r in rows:
        umap_2d = _json(r["umap_2d"])
        pca = _json(r["pca_coords"])
        tsne = _json(r["tsne_coords"])
        if umap_2d or pca or tsne:
            ready = True
        meta = _json(r["metadata"]) or {}
        chunks.append({
            "id": str(r["id"]),
            "document_name": r["document_name"],
            "path": str(r["path"]) if r["path"] is not None else None,
            "heading_trail": meta.get("heading_trail", []),
            "token_count": meta.get("token_count", 0),
            "study": _study_state(r["study_status"]),
            "coords": {"umap_2d": umap_2d, "pca": pca, "tsne": tsne},
        })
    return {"ready": ready, "chunks": chunks}


@router.post("/reset")
async def reset_study_progress(
    project_id: UUID,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Reset every chunk's study state back to ``not_studied`` for this project.

    A manual "start over" for the map: clears all actively/passively studied marks
    in one shot (see :func:`app.chat.study.clear_studied`). Returns how many chunks
    were actually cleared so the UI can report it.
    """
    await _owned_project(project_id, current_user["id"])
    cleared = await clear_studied(project_id)
    return {"cleared": cleared}


@router.get("/chunks/{chunk_id}")
async def get_chunk_content(
    project_id: UUID,
    chunk_id: UUID,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """The full Markdown text of one chunk, fetched lazily when a point is clicked.

    Kept out of ``get_projection`` on purpose — the map payload carries only the
    metadata needed to plot/label points, so a project's entire corpus isn't
    shipped up front. The join on ``project_id`` ensures a chunk from another
    project (or a stale id) 404s rather than leaking content across projects.
    """
    await _owned_project(project_id, current_user["id"])
    async with acquire() as conn:
        content = await conn.fetchval(
            """
            SELECT c.content_full
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE c.id = $1 AND d.project_id = $2
            """,
            chunk_id, project_id,
        )
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chunk not found")
    return {"id": str(chunk_id), "content_full": content}


@router.get("/search")
async def search_projection(
    project_id: UUID,
    q: str = Query(..., min_length=1, description="search term"),
    method: str = Query("cosine", pattern="^(cosine|bm25)$"),
    k: int = Query(5, ge=1, le=25, description="how many top matches to return"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Rank the project's chunks against ``q`` and return the top ``k`` ids + scores.

    The map rings exactly these top-``k`` nearest chunks — there is no relevance
    floor, so the user judges relevance from what gets ringed (an empty/unrelated
    project simply highlights its closest, possibly-irrelevant, chunks).

    * ``cosine`` — embed the query and order by pgvector cosine distance over the
      dense embedding (semantic match). Score is similarity in [0, 1].
    * ``bm25``   — Postgres full-text ``ts_rank`` over ``content_full`` (keyword
      match). Score is the raw rank; only chunks that match the query are returned.
    """
    await _owned_project(project_id, current_user["id"])

    if method == "cosine":
        [vector] = await embed([q])
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.id, 1 - (c.embedding <=> $2::halfvec) AS score
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE d.project_id = $1 AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> $2::halfvec
                LIMIT $3
                """,
                project_id, vector, k,
            )
    else:  # bm25 — keyword full-text ranking
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.id,
                       ts_rank(to_tsvector('english', c.content_full),
                               plainto_tsquery('english', $2)) AS score
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE d.project_id = $1
                  AND to_tsvector('english', c.content_full)
                      @@ plainto_tsquery('english', $2)
                ORDER BY score DESC
                LIMIT $3
                """,
                project_id, q, k,
            )

    return {
        "method": method,
        "query": q,
        "matches": [{"id": str(r["id"]), "score": float(r["score"])} for r in rows],
    }
