"""
Secrets that must not sit in the database or in a config file.

Two different things live here, stored two different ways, because they have
different threat models:

* **The OpenRouter API key** goes in the **OS keychain** (Windows Credential
  Manager, macOS Keychain, Secret Service on Linux) via ``keyring``. It is a
  live credential that can spend the user's money, and the OS already has a
  vetted place for exactly that.
* **The JWT signing secret** is a plain file in the app data dir, generated on
  first launch. It only signs a cookie for a server bound to localhost, it has to
  be readable by two sidecar processes before any user interaction, and it is
  disposable — regenerating it just logs the local user out.

What this module replaces: the old ``crypto.py`` Fernet layer. On a server,
encrypting the key column made sense — a database dump could leak to someone who
never had the server's environment. On a desktop, the DB, the key and the
would-be encryption key all sit on one disk under one user account, so
encrypting there protects against nothing while adding a key to lose.

No caching. The web process and the worker process are separate processes
reading one keychain; a cache in either would go stale the moment the user
changed their key in the other. Keychain reads are sub-millisecond.
"""
from __future__ import annotations

import logging
import os
import secrets as _secrets
import stat

import keyring
from keyring.errors import KeyringError

from app import paths

logger = logging.getLogger(__name__)

# Identifies our entries in the OS credential store. Renaming this orphans every
# key already stored by an installed copy — see the Phase 0 note on the app name.
SERVICE = "F7ows"

# Credentials are addressed by a short *key ref* ("openrouter", "openai", ...),
# which is what a provider role stores instead of the secret itself. The default
# ref keeps its original entry name so a key stored before per-role providers
# existed is still found rather than orphaned.
DEFAULT_REF = "openrouter"
_OPENROUTER = "openrouter_key"


def _entry(ref: str) -> str:
    return _OPENROUTER if ref == DEFAULT_REF else f"key:{ref}"


class KeystoreUnavailable(RuntimeError):
    """No usable OS keychain (headless Linux with no Secret Service, typically)."""


# ── provider keys ────────────────────────────────────────────────────────────
def get_key(ref: str = DEFAULT_REF) -> str | None:
    """The key stored under ``ref``, or None if there isn't one.

    A missing keychain backend reads the same as a missing key on purpose: the
    caller then falls back to the environment rather than failing outright, which
    keeps a headless dev box working.
    """
    if not ref:
        return None
    try:
        return keyring.get_password(SERVICE, _entry(ref)) or None
    except KeyringError as exc:
        logger.warning("keystore: cannot read the OS keychain (%s)", exc)
        return None


def set_key(key: str, ref: str = DEFAULT_REF) -> None:
    """Store (or replace) the key under ``ref``. Raises
    :class:`KeystoreUnavailable` if there is nowhere safe to put it — better a
    clear error than silently dropping a credential."""
    try:
        keyring.set_password(SERVICE, _entry(ref), key)
    except KeyringError as exc:
        raise KeystoreUnavailable(f"could not write to the OS keychain: {exc}") from exc


def clear_key(ref: str = DEFAULT_REF) -> None:
    """Remove the key stored under ``ref``. Absent is not an error — the end state
    is the same."""
    try:
        keyring.delete_password(SERVICE, _entry(ref))
    except KeyringError:
        pass


# Named aliases for the default ref, kept because most of the app only ever deals
# with the one OpenRouter key.
def get_openrouter_key() -> str | None:
    return get_key(DEFAULT_REF)


def set_openrouter_key(key: str) -> None:
    set_key(key, DEFAULT_REF)


def clear_openrouter_key() -> None:
    clear_key(DEFAULT_REF)


def mask(plain: str) -> str:
    """A non-secret preview for display, e.g. ``sk-or…3f9a``. Shows enough to
    recognise the key without revealing it."""
    if len(plain) <= 8:
        return "•" * len(plain)
    return f"{plain[:5]}…{plain[-4:]}"


# ── the JWT signing secret ───────────────────────────────────────────────────
_JWT_FILE = "jwt_secret"


def jwt_secret() -> str:
    """The JWT signing secret, generated on first launch and reused thereafter.

    Never ship a default: a secret baked into the binary would be identical on
    every install, so anyone could mint a valid cookie for anyone else's app.

    Created with ``O_EXCL`` because the web process and the worker start at the
    same moment and would otherwise each generate a secret and race to write it —
    whoever lost would sign cookies the other rejects. The loser of the race
    reads the winner's file instead.
    """
    path = paths.data_dir() / _JWT_FILE
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()

    value = _secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # no-op on Windows; matters on POSIX
    except OSError:
        pass
    logger.info("keystore: generated a new JWT signing secret at %s", path)
    return value
