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


@router.patch("/{project_id}")
async def update_project(project_id: str, request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    body = await request.json()
    # Build a partial update from the fields actually supplied.
    fields: list[str] = []
    args: list = []
    if "name" in body:
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Project name cannot be empty")
        fields.append(f"name = ${len(args) + 1}")
        args.append(name)
    if "description" in body:
        fields.append(f"description = ${len(args) + 1}")
        args.append(body.get("description"))
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to update")
    fields.append("updated_at = NOW()")
    args.extend([project_id, current_user["id"]])
    async with acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE projects SET {", ".join(fields)}
            WHERE id = ${len(args) - 1} AND user_id = ${len(args)}
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
            "DELETE FROM projects WHERE id = $1 AND user_id = $2 RETURNING id",
            project_id, current_user["id"],
        )
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
