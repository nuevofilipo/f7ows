"""
Authentication: registration, login, logout, current-user dependency.

JWT is issued as an HTTP-only cookie. Every protected route depends on
`get_current_user`, which decodes the cookie and loads the user row.

No pydantic here: request bodies are read straight from the JSON payload and
validated by hand, responses are plain dicts that FastAPI serialises for us.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app import apikey
from app.config import settings
from app.db import acquire

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _user_dict(row) -> dict:
    """Shape a users row into the internal current-user dict.

    Never includes the password hash. The user's OpenRouter key is kept encrypted
    under the underscore-prefixed ``_openrouter_key_enc`` (server-internal, stripped
    before any response by :func:`public_user`); ``openrouter_key_set`` is the
    non-secret boolean the frontend can show.

    asyncpg returns jsonb as a JSON string (no codec registered), so parse the
    settings blob back into a dict for callers (preferences resolution etc.).
    """
    settings_value = row["settings"]
    if isinstance(settings_value, str):
        try:
            settings_value = json.loads(settings_value)
        except (ValueError, TypeError):
            settings_value = {}
    enc = row["openrouter_key"] if "openrouter_key" in row else None
    return {
        "id": row["id"],
        "email": row["email"],
        "settings": settings_value if isinstance(settings_value, dict) else {},
        "created_at": row["created_at"],
        "_openrouter_key_enc": enc,
        "openrouter_key_set": bool(enc),
    }


def public_user(user: dict) -> dict:
    """Strip server-internal fields (encrypted secrets) before returning to a client."""
    return {k: v for k, v in user.items() if not k.startswith("_")}


async def _read_credentials(request: Request, *, min_password: int) -> tuple[str, str]:
    """Parse and validate {email, password} from the request body."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body")

    email = str(data.get("email", "")).strip().lower()
    password = data.get("password")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid email address")
    if not isinstance(password, str) or not (min_password <= len(password) <= 128):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Password must be between {min_password} and 128 characters",
        )
    return email, password


# ── Password hashing ─────────────────────────────────────────────────────────

_ph = PasswordHasher()
# Used to keep login timing roughly constant when the email doesn't exist.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$YWFhYWFhYWFhYWFhYWFhYQ$"
    "Yy7zN3qPbjWzMc/0hZkYG0fLqQ1pVZxFqYwGZf4w8bI"
)


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(hashed: str, plain: str) -> bool:
    try:
        _ph.verify(hashed, plain)
        return True
    except VerifyMismatchError:
        return False


# ── JWT helpers ──────────────────────────────────────────────────────────────

def create_access_token(user_id: UUID) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expiry_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> UUID:
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    return UUID(payload["sub"])


# ── Cookie helpers ─────────────────────────────────────────────────────────────
# The cookie is HTTP-only, so the browser stores it but JS can't read it; the
# backend is what sets it on login/register and clears it on logout.

def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.jwt_expiry_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )


# ── Current-user dependency ────────────────────────────────────────────────────

_UNAUTHORIZED = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise _UNAUTHORIZED
    try:
        user_id = decode_access_token(token)
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise _UNAUTHORIZED

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, settings, openrouter_key, created_at FROM users WHERE id = $1",
            user_id,
        )
    if row is None:
        raise _UNAUTHORIZED
    user = _user_dict(row)
    # Install this user's BYOK key for the whole request (and any task it spawns),
    # so every model call uses their key; falls back to the global key if unset.
    apikey.set_current_encrypted(user["_openrouter_key_enc"])
    return user


# OUTWARD POINTING ENDPOINTS STARTING FROM HERE:

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(request: Request, response: Response) -> dict:
    email, password = await _read_credentials(request, min_password=8)
    async with acquire() as conn:
        # ON CONFLICT lets us surface duplicate emails cleanly.
        row = await conn.fetchrow(
            """
            INSERT INTO users (email, hashed_password)
            VALUES ($1, $2)
            ON CONFLICT (email) DO NOTHING
            RETURNING id, email, settings, openrouter_key, created_at
            """,
            email,
            hash_password(password),
        )
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email is already registered")

    _set_auth_cookie(response, create_access_token(row["id"]))
    return public_user(_user_dict(row))


@router.post("/login")
async def login(request: Request, response: Response) -> dict:
    email, password = await _read_credentials(request, min_password=1)
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, hashed_password, settings, openrouter_key, created_at "
            "FROM users WHERE email = $1",
            email,
        )
    # Run verify even when the user is missing (against a dummy hash) so the
    # response time doesn't reveal whether the email exists.
    if row is None or not verify_password(row["hashed_password"], password):
        if row is None:
            verify_password(_DUMMY_HASH, password)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    _set_auth_cookie(response, create_access_token(row["id"]))
    return public_user(_user_dict(row))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(key=settings.cookie_name, path="/", domain=settings.cookie_domain)


@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)) -> dict:
    return public_user(current_user)
