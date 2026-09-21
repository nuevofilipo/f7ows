"""
Logging to a file in the app log directory.

A desktop app has no terminal to scroll back through: the sidecars are started by
the shell, and their stdout goes nowhere the user can reach. When something fails
at 11pm the only artifact is whatever was written to disk, so it has to be there
before anything interesting happens.

One file per process (``backend.log`` / ``worker.log``) rather than one shared
file: two processes appending to the same handle interleave mid-line, and the
rotation below would have each of them renaming the other's file underneath it.

Rotation is small on purpose. These logs are for "what happened just now", and an
unbounded log in the user's profile directory is a slow leak nobody notices.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app import paths

MAX_BYTES = 2 * 1024 * 1024      # 2 MB per file
BACKUP_COUNT = 3                 # ~8 MB per process, worst case
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_configured: str | None = None


def log_path(process: str) -> "paths.Path":
    """Where ``process`` writes: ``<LOGS>/<process>.log``."""
    return paths.log_dir() / f"{process}.log"


def setup(process: str, *, level: int = logging.INFO) -> "paths.Path":
    """Send this process's logging to a rotating file (and keep stderr).

    Idempotent: calling it twice returns the same path without stacking a second
    handler, which would double every line.
    """
    global _configured
    path = log_path(process)
    if _configured == process:
        return path

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(FORMAT)

    handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                                  encoding="utf-8", delay=True)
    handler.setFormatter(formatter)
    handler.set_name(f"f7ows-file-{process}")
    # Replace rather than append, so a re-entrant setup (tests, reload) doesn't
    # leave two handlers writing the same lines.
    for existing in list(root.handlers):
        # get_name() is None for a handler nobody named — e.g. the worker's
        # own basicConfig StreamHandler.
        if (existing.get_name() or "").startswith("f7ows-file-"):
            root.removeHandler(existing)
            existing.close()
    root.addHandler(handler)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in root.handlers):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    _configured = process
    logging.getLogger(__name__).info("logging to %s", path)
    return path


def tail(process: str, lines: int = 200) -> str:
    """The last ``lines`` lines of a process's log, for the in-app viewer.

    Reads the whole file — capped at a couple of megabytes by rotation, so the
    simple thing is also the correct thing here.
    """
    path = log_path(process)
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(could not read {path}: {exc})"
    return "\n".join(content.splitlines()[-lines:])
