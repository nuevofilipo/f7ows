"""Application settings, loaded once from the environment (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Postgres
    postgres_url: str
    # Redis
    redis_public_url: str
    # Bucket (Tigris / S3-compatible)
    bucket_endpoint_url: str
    bucket_region: str
    bucket_name: str
    bucket_access_key_id: str
    bucket_secret_access_key: str
    # Embeddings — OpenAI text-embedding-3-large, called via OpenRouter by default
    # so all model traffic stays on one key. EMBED_BASE_URL can be repointed at
    # OpenAI directly if the gateway doesn't proxy /embeddings.
    openrouter_key: str
    embed_base_url: str
    embed_model: str
    # LLM (chat) — the cheap "planner" for the agentic ltree-skeleton retrieval,
    # and the stronger model that writes chat answers.
    llm_base_url: str
    planner_model: str
    chat_model: str
    # Auth / JWT
    jwt_secret_key: str
    jwt_algorithm: str
    jwt_expiry_minutes: int
    # Fernet key for encrypting per-user secrets (BYOK OpenRouter keys) at rest.
    # A urlsafe-base64 32-byte key (`Fernet.generate_key()`); kept in env, never in
    # the DB, so a database leak alone can't decrypt stored keys. Optional: when
    # unset, users simply can't store a personal key (the global key is still used).
    fernet_key: str | None
    # Cookie — flip cookie_secure=True in production
    cookie_secure: bool
    cookie_samesite: str
    cookie_domain: str | None
    cookie_name: str
    # CORS — explicit list of allowed origins
    cors_origins: list[str]


settings = Settings(
    postgres_url=_required("POSTGRES_URL"),
    redis_public_url=_required("REDIS_PUBLIC_URL"),
    bucket_endpoint_url=_required("BUCKET_ENDPOINT_URL"),
    bucket_region=_required("BUCKET_REGION"),
    bucket_name=_required("BUCKET_NAME"),
    bucket_access_key_id=_required("BUCKET_ACCESS_KEY_ID"),
    bucket_secret_access_key=_required("BUCKET_SECRET_ACCESS_KEY"),
    openrouter_key=_required("OPENROUTER_KEY"),
    embed_base_url=os.getenv("EMBED_BASE_URL", "https://openrouter.ai/api/v1"),
    embed_model=os.getenv("EMBED_MODEL", "openai/text-embedding-3-large"),
    llm_base_url=os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
    planner_model=os.getenv("PLANNER_MODEL", "deepseek/deepseek-v4-flash"),
    chat_model=os.getenv("CHAT_MODEL", "deepseek/deepseek-v4-flash"),
    jwt_secret_key=_required("JWT_SECRET_KEY"),
    jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
    jwt_expiry_minutes=int(os.getenv("JWT_EXPIRY_MINUTES", str(60 * 24 * 7))),  # 7 days
    fernet_key=os.getenv("FERNET_KEY") or None,
    cookie_secure=_bool("COOKIE_SECURE", False),
    cookie_samesite=os.getenv("COOKIE_SAMESITE", "lax"),
    cookie_domain=os.getenv("COOKIE_DOMAIN") or None,
    cookie_name=os.getenv("COOKIE_NAME", "access_token"),
    cors_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()],
)
