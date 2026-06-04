"""
Symmetric encryption for per-user secrets at rest (currently: BYOK OpenRouter
keys).

We never store a user's API key in plaintext. Instead we Fernet-encrypt it with a
key (``FERNET_KEY``) that lives only in the server environment, never in the
database — so a database leak alone yields ciphertext, not working keys. Fernet
(from ``cryptography``) is authenticated symmetric encryption: one key both
encrypts and decrypts, and tampered tokens are rejected on decrypt.

The cipher is built lazily so the app still boots without ``FERNET_KEY`` set;
only the moment you try to encrypt/decrypt does it require one.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class CryptoUnavailable(RuntimeError):
    """Raised when an encryption op is attempted but FERNET_KEY isn't configured."""


_fernet: Fernet | None = None


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        if not settings.fernet_key:
            raise CryptoUnavailable(
                "FERNET_KEY is not configured; cannot store or read encrypted secrets"
            )
        _fernet = Fernet(settings.fernet_key)
    return _fernet


def encrypt(plain: str) -> str:
    """Encrypt ``plain`` into a urlsafe Fernet token (str). Raises CryptoUnavailable
    if no key is configured."""
    return _cipher().encrypt(plain.encode()).decode()


def decrypt(token: str | None) -> str | None:
    """Decrypt a Fernet ``token`` back to plaintext, or None if there's nothing to
    decrypt or the token can't be read (missing key, tampered/garbage value). Never
    raises — callers treat None as "no usable secret"."""
    if not token:
        return None
    try:
        return _cipher().decrypt(token.encode()).decode()
    except (InvalidToken, CryptoUnavailable, ValueError):
        return None


def mask(plain: str) -> str:
    """A non-secret preview of a key for display, e.g. ``sk-or…3f9a``. Shows enough
    to recognise the key without revealing it."""
    if len(plain) <= 8:
        return "•" * len(plain)
    return f"{plain[:5]}…{plain[-4:]}"
