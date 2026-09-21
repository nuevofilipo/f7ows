"""
Settings API — read and update the current user's preferences.

Preferences are stored as a flat JSON blob in users.settings and resolved
against app.preferences.preferences for effective values. GET returns the
effective settings plus the schema (so the UI can render the form and show
defaults/bounds); PUT validates a patch and merges it in.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app import keystore, llm, providers
from app.auth import get_current_user
from app.db import acquire
from app.preferences.preferences import describe, resolve, validate

router = APIRouter(prefix="/settings", tags=["settings"])


def _key_status() -> dict:
    """Non-secret view of the stored OpenRouter key: whether one is set and a
    masked preview. The plaintext key is never returned to the client — it goes
    into the OS keychain on write and only ever comes back out inside the
    Authorization header of a model call."""
    plain = keystore.get_openrouter_key()
    return {
        "openrouter_key_set": plain is not None,
        "openrouter_key_preview": keystore.mask(plain) if plain else None,
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
        **_key_status(),
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
            "UPDATE users SET settings = ? WHERE id = ?",
            json.dumps(merged), current_user["id"],
        )
    return {"effective": asdict(resolve(merged)), "overrides": merged, "schema": describe(),
            **_key_status()}


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
# Stored in the OS keychain (app/keystore.py), not in the database. The plaintext
# is write-only from the client's perspective (set here, never read back — GET
# returns only a preview).

@router.put("/openrouter-key")
async def set_openrouter_key(request: Request, current_user: dict = Depends(get_current_user)) -> dict:
    """Store (or replace) the OpenRouter key in the OS keychain."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body")
    key = body.get("key") if isinstance(body, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A non-empty key is required")
    key = key.strip()
    try:
        keystore.set_openrouter_key(key)
    except keystore.KeystoreUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"No OS keychain available to store the key: {exc}")
    return {"openrouter_key_set": True, "openrouter_key_preview": keystore.mask(key)}


@router.delete("/openrouter-key")
async def clear_openrouter_key(current_user: dict = Depends(get_current_user)) -> dict:
    """Remove the stored key — model calls fall back to the OPENROUTER_KEY env
    var if a dev checkout set one, and otherwise start failing with a message
    pointing back at this setting."""
    keystore.clear_openrouter_key()
    return {"openrouter_key_set": False, "openrouter_key_preview": None}


# ── Providers (per-role endpoints) ─────────────────────────────────────────────
# Each of chat / planner / embed resolves to {base_url, model, key_ref}, so the
# cheap planner can run on a local Ollama while chat stays on a frontier model.
# Stored in config.json; the secret itself stays in the OS keychain and only its
# `key_ref` is written here.

@router.get("/providers")
async def get_providers(current_user: dict = Depends(get_current_user)) -> dict:
    """Every role's effective provider config, plus whether its key resolves."""
    return {"providers": providers.describe(), "roles": list(providers.ROLES)}


@router.put("/providers/{role}")
async def set_provider(role: str, request: Request,
                       current_user: dict = Depends(get_current_user)) -> dict:
    """Update one role's provider. Only the fields present in the body are
    written, so anything the user hasn't pinned keeps following the app default.

    ``key_ref: ""`` is meaningful and accepted — it marks the endpoint as needing
    no auth at all, which is the normal case for a local Ollama.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Body must be a JSON object")

    patch = {k: body[k] for k in ("base_url", "model", "key_ref") if k in body}
    for field, value in patch.items():
        if not isinstance(value, str):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} must be a string")
    if "base_url" in patch and not patch["base_url"].strip().startswith(("http://", "https://")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "base_url must start with http:// or https://")
    try:
        providers.save(role, **patch)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return {"providers": providers.describe(), "roles": list(providers.ROLES)}


@router.put("/keys/{ref}")
async def set_provider_key(ref: str, request: Request,
                           current_user: dict = Depends(get_current_user)) -> dict:
    """Store a credential under a named ref, for a provider that isn't OpenRouter
    (a second gateway, OpenAI directly, ...). The OpenRouter key keeps its own
    endpoints above, since the profile page has always had a field for it."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body")
    key = body.get("key") if isinstance(body, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A non-empty key is required")
    try:
        keystore.set_key(key.strip(), ref)
    except keystore.KeystoreUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            f"No OS keychain available to store the key: {exc}")
    return {"ref": ref, "key_set": True, "key_preview": keystore.mask(key.strip())}


@router.delete("/keys/{ref}")
async def clear_provider_key(ref: str, current_user: dict = Depends(get_current_user)) -> dict:
    """Remove the credential stored under ``ref``."""
    keystore.clear_key(ref)
    return {"ref": ref, "key_set": False, "key_preview": None}
