"""
Projects API — a project is a user's notebook (its sources, chats and outputs).

Minimal create/list for now: enough to own documents and be the unit that
clustering/projections run over.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.auth import get_current_user
from app.db import NOW, acquire
from app.library import storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


def _project_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

# OUTWARD POINTING ENDPOINTS STARTING FROM HERE:

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Project name is required")
    description = body.get("description")
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO projects (id, user_id, name, description)
            VALUES (?, ?, ?, ?)
            RETURNING id, name, description, created_at, updated_at
            """,
            uuid4(), current_user["id"], name, description,
        )
    return _project_dict(row)


@router.get("")
async def list_projects(current_user: dict = Depends(get_current_user)) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description, created_at, updated_at
            FROM projects WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            current_user["id"],
        )
    return [_project_dict(r) for r in rows]


@router.patch("/{project_id}")
async def update_project(project_id: str, request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    body = await request.json()
    # Build a partial update from the fields actually supplied. Placeholders are
    # positional now, so the fragments no longer have to know their own index —
    # they just have to be appended in the same order as their arguments.
    fields: list[str] = []
    args: list = []
    if "name" in body:
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Project name cannot be empty")
        fields.append("name = ?")
        args.append(name)
    if "description" in body:
        fields.append("description = ?")
        args.append(body.get("description"))
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to update")
    fields.append(f"updated_at = {NOW}")
    args.extend([project_id, current_user["id"]])
    async with acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE projects SET {", ".join(fields)}
            WHERE id = ? AND user_id = ?
            RETURNING id, name, description, created_at, updated_at
            """,
            *args,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return _project_dict(row)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, current_user: dict = Depends(get_current_user)) -> None:
    # All children (documents → chunks, messages, clusters, artifacts) cascade
    # in the DB. Scope the delete by user_id so one user can't remove another's.
    async with acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM projects WHERE id = ? AND user_id = ? RETURNING id",
            project_id, current_user["id"],
        )
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")


@router.get("/{project_id}/export")
async def export_project(project_id: str, current_user: dict = Depends(get_current_user)) -> Response:
    """Download one project as a zip: its rows as JSON, plus the original files.

    Why this exists at all: the whole database is one file, so "back up F7ows" is
    already trivial — but "send me that notebook" isn't, and neither is keeping a
    project after uninstalling. This is the per-project unit.

    The source PDFs are included rather than referenced. An export whose documents
    live somewhere else isn't a backup; it's a manifest of things you can lose
    separately.

    Chunks and embeddings are deliberately *not* included. They are derived — a
    re-ingest reproduces them — and they are almost all of the bytes: a 4 KB
    vector per chunk would dwarf the PDF it came from.
    """
    async with acquire() as conn:
        project = await conn.fetchrow(
            "SELECT id, name, description, created_at, updated_at FROM projects "
            "WHERE id = ? AND user_id = ?",
            project_id, current_user["id"],
        )
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        documents = await conn.fetch(
            "SELECT id, name, storage_key, content_type, size_bytes, status, created_at "
            "FROM documents WHERE project_id = ? ORDER BY created_at",
            project_id,
        )
        # There is no chats table — a chat is just the set of messages sharing a
        # chat_id (see app/migrations.py), so the export derives the list.
        chats = await conn.fetch(
            "SELECT chat_id, count(*) AS messages, min(created_at) AS created_at "
            "FROM messages WHERE project_id = ? GROUP BY chat_id ORDER BY created_at",
            project_id,
        )
        messages = await conn.fetch(
            "SELECT id, chat_id, role, content, metadata, path, created_at "
            "FROM messages WHERE project_id = ? ORDER BY path",
            project_id,
        )

    manifest = {
        "format": "f7ows-project-export/1",
        "project": _project_dict(project),
        "documents": [dict(d) for d in documents],
        "chats": [dict(c) for c in chats],
        "messages": [dict(m) for m in messages],
    }

    files: list[tuple[str, bytes]] = []
    for doc in documents:
        try:
            files.append((doc["name"], await storage.download_bytes(doc["storage_key"])))
        except (FileNotFoundError, ValueError) as exc:
            # A missing source file must not fail the whole export — the rest of
            # the project is still worth having, and the manifest records it.
            logger.warning("export: skipping %s (%s)", doc["storage_key"], exc)
            manifest.setdefault("missing_files", []).append(doc["name"])

    def _build() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("project.json", json.dumps(manifest, indent=2, default=str))
            seen: dict[str, int] = {}
            for name, data in files:
                # Two documents can share a filename; the DB kept them apart by id,
                # a zip entry has only its name.
                if name in seen:
                    seen[name] += 1
                    stem, _, ext = name.rpartition(".")
                    name = f"{stem} ({seen[name]}).{ext}" if stem else f"{name} ({seen[name]})"
                else:
                    seen[name] = 0
                zf.writestr(f"documents/{name}", data)
        return buf.getvalue()

    blob = await asyncio.to_thread(_build)
    safe_name = "".join(c for c in (project["name"] or "project") if c.isalnum() or c in " -_").strip()
    filename = f"{safe_name or 'project'}.f7ows.zip"
    logger.info("export: %s (%d documents, %d bytes)", project_id, len(files), len(blob))
    return Response(
        content=blob,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
