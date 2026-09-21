"""
Where F7ows keeps its files.

A desktop app can't write next to its own executable: on Windows it lands in
Program Files (read-only for a normal user), and on macOS it lives inside a
signed .app bundle where writing breaks the code signature. So everything the app
produces goes into per-user directories the OS sets aside for it.

Three of them, split by *what happens if the directory is deleted*:

* **data** — irreplaceable. The database and the user's uploaded documents.
  Deleting it loses work; this is what a backup has to cover.
* **cache** — regenerable, and potentially large: the opt-in Docling model
  download and the trace runs. Deleting it costs a re-download or a re-run.
* **logs** — diagnostics. Deleting it costs nothing.

The split is not cosmetic. If the multi-gigabyte model download lived under data,
every backup the user took would drag it along.

Resolution order, highest priority first:

1. ``F7OWS_DATA_DIR`` / ``F7OWS_CACHE_DIR`` / ``F7OWS_LOG_DIR``. The Tauri shell
   resolves the real OS paths and passes them to the Python sidecars this way, so
   the shell stays the single source of truth and both processes agree. Also the
   hook for pointing a test run at a scratch directory.
2. ``./devdata/`` under the backend directory, when running from a source
   checkout. Keeps development from ever touching an installed copy's real data.
3. The OS conventions below, when frozen (PyInstaller sets ``sys.frozen``).

On Windows that means ``%LOCALAPPDATA%``, deliberately not ``%APPDATA%``:
%APPDATA% is the *Roaming* profile, which on domain-joined machines is
synchronised to a server at login. A multi-gigabyte database there would be a
catastrophe for whoever administers that network.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "F7ows"

# The backend/ directory — i.e. the parent of app/.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _frozen() -> bool:
    """True when running from a PyInstaller bundle rather than a source tree."""
    return bool(getattr(sys, "frozen", False))


def _os_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME.lower()


def _os_cache_dir() -> Path:
    if sys.platform == "win32":
        return _os_data_dir() / "cache"          # Windows has no separate cache root
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_NAME
    base = os.getenv("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / APP_NAME.lower()


def _os_log_dir() -> Path:
    if sys.platform == "win32":
        return _os_data_dir() / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP_NAME
    base = os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / APP_NAME.lower() / "logs"


def _resolve(env_var: str, os_default, dev_subdir: str) -> Path:
    override = os.getenv(env_var)
    if override:
        return Path(override).expanduser()
    if _frozen():
        return os_default()
    return _BACKEND_ROOT / "devdata" / dev_subdir


def data_dir() -> Path:
    """Irreplaceable user content: the database and uploaded documents."""
    return _ensure(_resolve("F7OWS_DATA_DIR", _os_data_dir, "data"))


def cache_dir() -> Path:
    """Regenerable and possibly large: downloaded models, trace runs."""
    return _ensure(_resolve("F7OWS_CACHE_DIR", _os_cache_dir, "cache"))


def log_dir() -> Path:
    """Diagnostics."""
    return _ensure(_resolve("F7OWS_LOG_DIR", _os_log_dir, "logs"))


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    """The SQLite database file (its -wal and -shm siblings sit next to it)."""
    return data_dir() / "db.sqlite3"


def documents_dir() -> Path:
    """Uploaded source files. ``documents.storage_key`` is stored relative to
    :func:`data_dir` and already carries this directory as its first segment
    (``documents/<project_id>/<document_id>/<name>``) — the same layout the S3
    bucket used, so the stored keys need no migration. Created here so the
    directory exists even before the first upload; ``library/storage.py``
    resolves keys against :func:`data_dir`, not against this."""
    return _ensure(data_dir() / "documents")


def runs_dir() -> Path:
    """Execution traces (app/trace.py). Cache, not data: unbounded growth, and
    losing old runs costs nothing."""
    return _ensure(cache_dir() / "runs")


def models_dir() -> Path:
    """The opt-in Docling/HuggingFace model download. Gigabytes, re-fetchable."""
    return _ensure(cache_dir() / "models")


def describe() -> str:
    """One-line-per-directory summary, for logging at startup and for the harness."""
    mode = "frozen" if _frozen() else "source checkout"
    lines = [f"F7ows paths ({mode}):"]
    for label, value in (
        ("data", data_dir()), ("  db", db_path()), ("  documents", documents_dir()),
        ("cache", cache_dir()), ("  runs", runs_dir()), ("  models", models_dir()),
        ("logs", log_dir()),
    ):
        lines.append(f"  {label:12} {value}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
