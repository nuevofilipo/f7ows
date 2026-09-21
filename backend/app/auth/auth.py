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
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.config import settings
from app.db import acquire
from app.local_user import ensure_local_user

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _user_dict(row) -> dict:
    """Shape a users row into the internal current-user dict.

    Never includes the password hash. The OpenRouter key is no longer part of the
    user at all — it lives in the OS keychain (:mod:`app.keystore`), one key for
    the one local user, so nothing here has to carry it around.

    SQLite has no JSON type — ``settings`` is TEXT holding a JSON object — so
    parse the blob back into a dict for callers (preferences resolution etc.).
    """
    settings_value = row["settings"]
    if isinstance(settings_value, str):
        try:
            settings_value = json.loads(settings_value)
        except (ValueError, TypeError):
            settings_value = {}
    return {
        "id": row["id"],
        "email": row["email"],
        "settings": settings_value if isinstance(settings_value, dict) else {},
        "created_at": row["created_at"],
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
    """Resolve the request's user.

    In single-user (desktop) mode a missing or unreadable cookie is not an error:
    it resolves to the seeded local user. There is nobody else to be, and the
    alternative — 401 until the frontend completes a login it has no credentials
    for — would just be a lock on a door with one key taped to it.

    That also makes auth independent of whether the cookie survives the trip.
    The page and the API sit on different origins in the desktop shell (the shell
    binds the backend to its own port), which is exactly the cross-site case
    browsers keep tightening; the cookie is still issued and still honoured when
    it arrives, but nothing depends on it arriving.
    """
    user_id = None
    token = request.cookies.get(settings.cookie_name)
    if token:
        try:
            user_id = decode_access_token(token)
        except (jwt.InvalidTokenError, KeyError, ValueError):
            user_id = None

    if user_id is None:
        if not settings.single_user:
            raise _UNAUTHORIZED
        user_id = await ensure_local_user()

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, settings, created_at FROM users WHERE id = ?",
            user_id,
        )
    if row is None:
        raise _UNAUTHORIZED
    return _user_dict(row)


# OUTWARD POINTING ENDPOINTS STARTING FROM HERE:

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(request: Request, response: Response) -> dict:
    email, password = await _read_credentials(request, min_password=8)
    async with acquire() as conn:
        # ON CONFLICT lets us surface duplicate emails cleanly.
        row = await conn.fetchrow(
            """
            INSERT INTO users (id, email, hashed_password)
            VALUES (?, ?, ?)
            ON CONFLICT (email) DO NOTHING
            RETURNING id, email, settings, created_at
            """,
            uuid4(),
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
            "SELECT id, email, hashed_password, settings, created_at "
            "FROM users WHERE email = ?",
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
async def me(response: Response, current_user: dict = Depends(get_current_user)) -> dict:
    """The current user — and, in single-user mode, the place the cookie gets
    auto-issued. The frontend calls this once on boot, so the local user ends up
    with a normal session cookie without ever seeing a login form, and the
    /auth/login route stays untouched for when sync gives it a reason to exist."""
    if settings.single_user:
        _set_auth_cookie(response, create_access_token(current_user["id"]))
    # The frontend needs to know which mode it is in — it hides "Log out" and the
    # login form when there is only ever one user to be.
    return {**public_user(current_user), "single_user": settings.single_user}
