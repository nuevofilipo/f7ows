"""
Per-role provider configuration.

The app makes three kinds of model call, and they don't have to go to the same
place:

* **chat** — the model that writes answers. The expensive one.
* **planner** — the cheap classifier behind agentic retrieval. Many small calls.
* **embed** — vectors for retrieval.

Each role resolves to ``{base_url, model, key_ref}``, which is enough to talk to
any OpenAI-compatible endpoint: OpenRouter (the default), OpenAI directly, or a
local Ollama at ``http://localhost:11434/v1``. Splitting by role is the point —
running the planner locally while chat stays on a frontier model is the
configuration most people actually want, and it is the one a single global
"provider" setting cannot express.

``key_ref`` names a credential in the OS keychain rather than holding one, so
config.json stays free of secrets and can be copied, diffed or backed up safely.
An empty ``key_ref`` means the endpoint needs no auth at all, which is the normal
case for Ollama — and *not* the same as "the key is missing", which is an error.

Resolution per field, highest priority first:

1. The role's environment variable (``CHAT_MODEL``, ``LLM_BASE_URL``, ...) — the
   dev override, and what the cloud deployment used.
2. ``providers.<role>`` in ``config.json`` — what the settings UI writes.
3. The app default (OpenRouter).

config.json is re-read when its mtime changes rather than cached for the life of
the process: the web app writes it and the *worker* has to see the change, and a
user who repoints their embed provider should not have to restart the app to make
the next ingest use it.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app import config, keystore, paths
from app.config import settings

logger = logging.getLogger(__name__)

ROLES = ("chat", "planner", "embed")

# Per role: the env var for the base URL, the env var for the model, and the
# settings attribute holding the default for each.
_ENV = {
    "chat":    ("LLM_BASE_URL", "CHAT_MODEL", "llm_base_url", "chat_model"),
    "planner": ("LLM_BASE_URL", "PLANNER_MODEL", "llm_base_url", "planner_model"),
    "embed":   ("EMBED_BASE_URL", "EMBED_MODEL", "embed_base_url", "embed_model"),
}


class MissingAPIKey(RuntimeError):
    """A role needs a key and none is stored. Surfaced as "add your key in Settings"."""


@dataclass(frozen=True)
class Provider:
    """A resolved endpoint for one role."""

    role: str
    base_url: str
    model: str
    key_ref: str

    def url(self, path: str) -> str:
        """Full URL for an OpenAI-compatible path, e.g. ``/chat/completions``."""
        return f"{self.base_url.rstrip('/')}{path}"

    def key(self) -> str | None:
        """The credential for this role, or None when the endpoint needs none.

        Falls back to ``OPENROUTER_KEY`` from the environment for the default
        ref so a dev checkout keeps working without touching the keychain.
        """
        if not self.key_ref:
            return None
        key = keystore.get_key(self.key_ref)
        if not key and self.key_ref == keystore.DEFAULT_REF:
            key = settings.openrouter_key or None
        if not key:
            raise MissingAPIKey(
                f"The {self.role} provider is configured to use the '{self.key_ref}' key, "
                "but none is stored. Add it in Settings."
            )
        return key

    def auth_header(self) -> dict[str, str]:
        """``Authorization`` header, or no header at all for a keyless endpoint.

        Ollama ignores the header, but a bare ``Bearer`` with an empty value is
        worse than sending nothing: some proxies reject it outright.
        """
        key = self.key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    @property
    def keyless(self) -> bool:
        return not self.key_ref


# ── resolution ───────────────────────────────────────────────────────────────
_block: dict = {}                     # the providers block of config.json
_resolved: dict[str, Provider] = {}   # per-role, derived from _block + env
_stamp: tuple | None = None           # (mtime_ns, size) the above were read at


def _providers_config() -> dict:
    """The ``providers`` block of config.json, re-read when the file changes.

    Stamped on mtime+size rather than cached for the process lifetime: the web
    app writes this file and the *worker* is a second process that has to notice,
    and repointing a provider shouldn't need an app restart to take effect.
    """
    global _block, _resolved, _stamp
    path = paths.data_dir() / config.CONFIG_FILE
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    if stamp != _stamp:
        _stamp = stamp
        _resolved = {}                # the file changed; re-derive every role
        raw = config._load_config_json().get("providers")
        _block = raw if isinstance(raw, dict) else {}
    return _block


def for_role(role: str) -> Provider:
    """Resolve one role. Unknown roles fall back to ``planner`` (the cheap one),
    so a typo costs money slowly rather than quickly."""
    if role not in ROLES:
        logger.warning("providers: unknown role %r, using planner", role)
        role = "planner"

    block = _providers_config()
    if role in _resolved:
        return _resolved[role]

    base_env, model_env, base_attr, model_attr = _ENV[role]
    stored = block.get(role) if isinstance(block.get(role), dict) else {}

    base_url = os.getenv(base_env) or stored.get("base_url") or getattr(settings, base_attr)
    model = os.getenv(model_env) or stored.get("model") or getattr(settings, model_attr)
    # key_ref is deliberately not env-overridable: it names a keychain entry, and
    # `""` is a meaningful value (keyless), which `os.getenv(...) or ...` would
    # silently swallow.
    key_ref = stored.get("key_ref")
    if key_ref is None:
        key_ref = keystore.DEFAULT_REF

    provider = Provider(role=role, base_url=str(base_url), model=str(model), key_ref=str(key_ref))
    _resolved[role] = provider
    return provider


def describe() -> list[dict]:
    """Every role's effective config, plus whether its key is available — for the
    settings UI. Never includes a key, only whether one resolves."""
    out = []
    for role in ROLES:
        p = for_role(role)
        try:
            has_key = p.key() is not None
        except MissingAPIKey:
            has_key = False
        out.append({
            "role": role,
            "base_url": p.base_url,
            "model": p.model,
            "key_ref": p.key_ref,
            "keyless": p.keyless,
            "key_available": has_key,
        })
    return out


def save(role: str, *, base_url: str | None = None, model: str | None = None,
         key_ref: str | None = None) -> dict:
    """Persist a role's provider settings into config.json and drop the cache.

    Only the fields given are written, so a role keeps following the app default
    for anything the user hasn't explicitly pinned.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    block = dict(config._load_config_json().get("providers") or {})
    entry = dict(block.get(role) or {})
    for field, value in (("base_url", base_url), ("model", model), ("key_ref", key_ref)):
        if value is not None:
            entry[field] = value.strip()
    block[role] = entry
    config.write_config_json({"providers": block})
    _invalidate()
    return entry


def _invalidate() -> None:
    """Forget everything derived from config.json (after a write, or in tests)."""
    global _block, _resolved, _stamp
    _block, _resolved, _stamp = {}, {}, None
