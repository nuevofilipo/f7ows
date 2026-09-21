"""
Source-document storage — files on the local disk, under the app data directory.

The desktop build has no bucket: ``storage_key`` is simply a relative path below
``paths.data_dir()``, so the key the database has always stored
(``documents/<project_id>/<document_id>/<name>.pdf``) maps straight onto a real
file without a migration.

File IO is blocking, so each call still runs in a threadpool — the async
signatures are unchanged from the S3 version, and callers never noticed the swap.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app import paths

logger = logging.getLogger(__name__)


def _resolve(key: str) -> Path:
    """Map a storage key onto an absolute path, refusing anything that escapes
    the data directory.

    Keys are built from user-supplied filenames, so this is the one place that
    has to care: an absolute path, a Windows drive letter or a ``..`` segment
    would otherwise let an upload write anywhere the user can write. The check is
    done on the resolved path rather than on the string, which also catches
    symlinks pointing outside the tree.
    """
    root = paths.data_dir().resolve()
    candidate = (root / key).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"storage key escapes the data directory: {key!r}")
    return candidate


def _write(key: str, data: bytes) -> None:
    path = _resolve(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a crash mid-write leaves the old file intact rather than
    # a truncated PDF that later fails to parse.
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


async def upload_bytes(key: str, data: bytes, content_type: str | None = None) -> None:
    """Store ``data`` at ``key``.

    ``content_type`` is accepted and ignored: the filesystem has nowhere to put
    it, and the database column already records it.
    """
    await asyncio.to_thread(_write, key, data)
    logger.info("storage: wrote %s (%d bytes)", key, len(data))


async def download_bytes(key: str) -> bytes:
    """Read the file at ``key`` (used by the ingest worker).

    Raises ``FileNotFoundError`` if it is gone — the worker turns that into an
    error status on the document.
    """
    return await asyncio.to_thread(lambda: _resolve(key).read_bytes())
