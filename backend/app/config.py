"""
Application settings.

Three sources, highest priority first:

1. **Environment / .env** — the developer's override. Kept first so a dev can
   point a source checkout at anything without touching the user's real config.
2. **``config.json``** in the app data dir — what the app itself writes when the
   user changes something in the UI. Never holds secrets: the OpenRouter key is
   in the OS keychain (:mod:`app.keystore`) and the JWT secret is its own file.
3. **The defaults below.**

Nothing here is `_required()` any more. On a server a missing variable should
stop the process — the operator can read logs and fix the environment. A desktop
app has no operator: it has to boot, then tell the user what to fill in. The one
value that genuinely cannot have a default (the JWT secret) is generated on first
launch rather than demanded.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

from app import keystore, paths

logger = logging.getLogger(__name__)

# Source checkouts only. python-dotenv's find_dotenv() has an explicit
# `getattr(sys, "frozen", False)` branch that searches upward from **os.getcwd()**
# rather than from the bundle — so a frozen F7ows launched from a directory that
# happens to sit above a .env would silently load it, and env beats config.json
# here. An installed copy must read the user's config and nothing else.
if not getattr(sys, "frozen", False):
    load_dotenv()

CONFIG_FILE = "config.json"


def _load_config_json() -> dict:
    """User-facing settings the app writes for itself. A corrupt file is logged
    and ignored — a stray character in a JSON file must not brick the app."""
    path = paths.data_dir() / CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        logger.warning("config: ignoring unreadable %s (%s)", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


_file_config = _load_config_json()


def _get(key: str, default: str) -> str:
    """env -> config.json -> default, for a string setting. The env var and the
    JSON key are the same name, lowercased for JSON (``EMBED_MODEL`` /
    ``embed_model``)."""
    env = os.getenv(key)
    if env:
        return env
    value = _file_config.get(key.lower())
    return str(value) if value not in (None, "") else default


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        raw = _file_config.get(key.lower())
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Embeddings — OpenAI text-embedding-3-large, called via OpenRouter by default
    # so all model traffic stays on one key. EMBED_BASE_URL can be repointed at
    # OpenAI directly if the gateway doesn't proxy /embeddings.
    embed_base_url: str
    embed_model: str
    # LLM (chat) — the cheap "planner" for the agentic skeleton retrieval, and the
    # stronger model that writes chat answers.
    llm_base_url: str
    planner_model: str
    chat_model: str
    # Fallback OpenRouter key for a dev checkout. The real key lives in the OS
    # keychain; this is only consulted when the keychain holds nothing, so a
    # developer can export OPENROUTER_KEY and skip the settings UI entirely.
    openrouter_key: str
    # Auth / JWT. The secret is generated into the app data dir on first launch.
    jwt_algorithm: str
    jwt_expiry_minutes: int
    # Single local user: seed one on first run and treat every request as that
    # user, so the desktop app has no login screen. Set SINGLE_USER=0 to get the
    # multi-user cloud behaviour back (the /auth routes are still registered).
    single_user: bool
    single_user_email: str
    # Cookie — flip cookie_secure=True in production
    cookie_secure: bool
    cookie_samesite: str
    cookie_domain: str | None
    cookie_name: str
    # CORS — explicit list of allowed origins
    cors_origins: list[str]

    @property
    def jwt_secret_key(self) -> str:
        """Read through to :func:`app.keystore.jwt_secret`.

        A property, not a field, because the first read *generates and writes* a
        secret. Reading config.json at import is one thing; minting a credential
        is another, and only a process that actually authenticates should do it.
        """
        return keystore.jwt_secret()


settings = Settings(
    embed_base_url=_get("EMBED_BASE_URL", "https://openrouter.ai/api/v1"),
    embed_model=_get("EMBED_MODEL", "openai/text-embedding-3-large"),
    llm_base_url=_get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
    planner_model=_get("PLANNER_MODEL", "deepseek/deepseek-v4-flash"),
    chat_model=_get("CHAT_MODEL", "deepseek/deepseek-v4-flash"),
    openrouter_key=os.getenv("OPENROUTER_KEY", ""),
    jwt_algorithm=_get("JWT_ALGORITHM", "HS256"),
    jwt_expiry_minutes=int(_get("JWT_EXPIRY_MINUTES", str(60 * 24 * 7))),  # 7 days
    single_user=_bool("SINGLE_USER", True),
    single_user_email=_get("SINGLE_USER_EMAIL", "local@f7ows.app"),
    cookie_secure=_bool("COOKIE_SECURE", False),
    cookie_samesite=_get("COOKIE_SAMESITE", "lax"),
    cookie_domain=os.getenv("COOKIE_DOMAIN") or None,
    cookie_name=_get("COOKIE_NAME", "access_token"),
    cors_origins=[o.strip() for o in _get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()],
)


def write_config_json(patch: dict) -> dict:
    """Merge ``patch`` into ``config.json`` and return the new contents.

    Writes only what the user changed, so a future version adding a default
    doesn't find it frozen into every existing install. Does not touch the
    in-memory ``settings`` — those are read once at import, and the caller is
    expected to say a restart is needed.
    """
    path = paths.data_dir() / CONFIG_FILE
    merged = {**_load_config_json(), **patch}
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return merged
