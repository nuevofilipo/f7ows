"""
Projects API — a project is a user's notebook (its sources, chats and outputs).

Minimal create/list for now: enough to own documents and be the unit that
clustering/projections run over.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import get_current_user
from app.db import acquire

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
            INSERT INTO projects (user_id, name, description)
            VALUES ($1, $2, $3)
            RETURNING id, name, description, created_at, updated_at
            """,
            current_user["id"], name, description,
        )
    return _project_dict(row)


@router.get("")
async def list_projects(current_user: dict = Depends(get_current_user)) -> list[dict]:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description, created_at, updated_at
            FROM projects WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            current_user["id"],
        )
    return [_project_dict(r) for r in rows]
