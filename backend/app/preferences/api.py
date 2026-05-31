"""
Settings API — read and update the current user's preferences.

Preferences are stored as a flat JSONB blob in users.settings and resolved
against app.preferences.preferences for effective values. GET returns the
effective settings plus the schema (so the UI can render the form and show
defaults/bounds); PUT validates a patch and merges it in.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import get_current_user
from app.db import acquire
from app.preferences.preferences import describe, resolve, validate

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings(current_user: dict = Depends(get_current_user)) -> dict:
    """Effective (resolved) settings, the raw stored overrides, and the schema."""
    stored = current_user["settings"] if isinstance(current_user["settings"], dict) else {}
    return {
        "effective": asdict(resolve(stored)),
        "overrides": stored,
        "schema": describe(),
    }


@router.put("")
async def update_settings(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    """Merge a settings patch into the user's stored overrides (validated/clamped)."""
    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body")
    if not isinstance(patch, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Body must be a JSON object")
    try:
        cleaned = validate(patch)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    stored = current_user["settings"] if isinstance(current_user["settings"], dict) else {}
    merged = {**stored, **cleaned}
    async with acquire() as conn:
        await conn.execute(
            "UPDATE users SET settings = $2::jsonb WHERE id = $1",
            current_user["id"], json.dumps(merged),
        )
    return {"effective": asdict(resolve(merged)), "overrides": merged, "schema": describe()}
