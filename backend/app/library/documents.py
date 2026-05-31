"""
Documents API — upload a PDF source into a project.

Upload flow (PHASE4_PLAN_v2 §1): save the PDF to the bucket, insert a `documents`
row in state "uploaded", and (step 5) enqueue ingest_document. List exists so you
can verify what landed.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status

from app.auth import get_current_user
from app.db import acquire
from app.producer import enqueue_finalize, enqueue_ingest
from app.library import storage

router = APIRouter(prefix="/projects/{project_id}/documents", tags=["documents"])

_MAX_BYTES = 50 * 1024 * 1024  # 50 MB upload cap, WHY is this necessary, could we allow bigger pdfs to be uploaded?


def _document_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "storage_key": row["storage_key"],
        "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


async def _owned_project(project_id: UUID, user_id) -> None:
    """404 unless the project exists and belongs to the current user."""
    async with acquire() as conn:
        owner = await conn.fetchval(
            "SELECT 1 FROM projects WHERE id = $1 AND user_id = $2",
            project_id, user_id,
        )
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")


async def _store_and_enqueue(project_id: UUID, file: UploadFile) -> dict:
    """Validate one PDF, save it to the bucket, insert the row, and enqueue the
    full ingest pipeline (parse -> chunk -> embed -> project -> cluster). Returns
    the created document. Raises HTTPException on a bad file."""
    if (file.content_type or "") != "application/pdf" and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only PDF uploads are supported")

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Empty file")
    if len(data) > _MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File exceeds 50 MB limit")

    document_id = uuid4()
    name = file.filename or f"{document_id}.pdf"
    storage_key = f"documents/{project_id}/{document_id}/{name}"
    await storage.upload_bytes(storage_key, data, "application/pdf")

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO documents (id, project_id, name, storage_key, content_type, size_bytes)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, name, storage_key, content_type, size_bytes, status, created_at
            """,
            document_id, project_id, name, storage_key, "application/pdf", len(data),
        )

    await enqueue_ingest(document_id)
    return _document_dict(row)

# OUTWARD POINTING ENDPOINTS STARTING FROM HERE:

@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_document(
    project_id: UUID,
    file: UploadFile,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Upload a single PDF and kick off its ingest pipeline."""
    await _owned_project(project_id, current_user["id"])
    doc = await _store_and_enqueue(project_id, file)
    await enqueue_finalize(project_id, [doc["id"]])
    return doc


@router.post("/batch", status_code=status.HTTP_201_CREATED)
async def upload_documents(
    project_id: UUID,
    files: list[UploadFile],
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Upload several PDFs at once; each runs the full pipeline independently.

    Per-file errors don't abort the batch — the response lists the documents that
    were accepted plus any files that were rejected and why.
    """
    await _owned_project(project_id, current_user["id"])
    uploaded, errors = [], []
    for file in files:
        try:
            uploaded.append(await _store_and_enqueue(project_id, file))
        except HTTPException as exc:
            errors.append({"filename": file.filename, "error": exc.detail})
    if uploaded:
        # One finalize for the whole batch -> projections + clusters recomputed once,
        # after every uploaded document has finished ingesting.
        await enqueue_finalize(project_id, [d["id"] for d in uploaded])
    return {"uploaded": uploaded, "errors": errors}


@router.get("")
async def list_documents(
    project_id: UUID,
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    await _owned_project(project_id, current_user["id"])
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, storage_key, content_type, size_bytes, status, created_at
            FROM documents WHERE project_id = $1
            ORDER BY created_at DESC
            """,
            project_id,
        )
    return [_document_dict(r) for r in rows]
