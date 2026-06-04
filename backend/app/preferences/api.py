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

from app import crypto, llm
from app.auth import get_current_user
from app.db import acquire
from app.preferences.preferences import describe, resolve, validate

router = APIRouter(prefix="/settings", tags=["settings"])


def _key_status(current_user: dict) -> dict:
    """Non-secret view of the user's stored OpenRouter key: whether one is set and
    a masked preview. The plaintext key is never returned to the client."""
    plain = crypto.decrypt(current_user.get("_openrouter_key_enc"))
    return {
        "openrouter_key_set": plain is not None,
        "openrouter_key_preview": crypto.mask(plain) if plain else None,
    }


@router.get("")
async def get_settings(current_user: dict = Depends(get_current_user)) -> dict:
    """Effective (resolved) settings, the raw stored overrides, the schema, and the
    masked status of the user's OpenRouter key."""
    stored = current_user["settings"] if isinstance(current_user["settings"], dict) else {}
    return {
        "effective": asdict(resolve(stored)),
        "overrides": stored,
        "schema": describe(),
        **_key_status(current_user),
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
    return {"effective": asdict(resolve(merged)), "overrides": merged, "schema": describe(),
            **_key_status(current_user)}


# ── Model catalogue ─────────────────────────────────────────────────────────────
# Fetched live from the gateway each time the model dropdown opens (no cache, by
# design) — trimmed to id + display name so the client gets a small payload.

@router.get("/models")
async def list_models(current_user: dict = Depends(get_current_user)) -> dict:
    """The models available on the gateway, sorted by display name."""
    try:
        raw = await llm.list_models()
    except llm.LLMError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Could not list models: {exc}")
    models = [{"id": m["id"], "name": m.get("name") or m["id"]} for m in raw if m.get("id")]
    models.sort(key=lambda m: m["name"].lower())
    return {"models": models}


# ── OpenRouter key (BYOK) ──────────────────────────────────────────────────────
# Stored Fernet-encrypted in users.openrouter_key; the plaintext is write-only from
# the client's perspective (set here, never read back — GET returns only a preview).

@router.put("/openrouter-key")
async def set_openrouter_key(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    """Store (or replace) the user's OpenRouter key, encrypted at rest."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body")
    key = body.get("key") if isinstance(body, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A non-empty key is required")
    key = key.strip()
    try:
        encrypted = crypto.encrypt(key)
    except crypto.CryptoUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Server is not configured to store keys (missing FERNET_KEY)")
    async with acquire() as conn:
        await conn.execute(
            "UPDATE users SET openrouter_key = $2 WHERE id = $1",
            current_user["id"], encrypted,
        )
    return {"openrouter_key_set": True, "openrouter_key_preview": crypto.mask(key)}


@router.delete("/openrouter-key")
async def clear_openrouter_key(current_user: dict = Depends(get_current_user)) -> dict:
    """Remove the user's stored key — their traffic falls back to the global key."""
    async with acquire() as conn:
        await conn.execute(
            "UPDATE users SET openrouter_key = NULL WHERE id = $1", current_user["id"],
        )
    return {"openrouter_key_set": False, "openrouter_key_preview": None}
