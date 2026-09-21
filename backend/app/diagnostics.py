"""
Diagnostics API — the logs and the execution traces, readable from inside the app.

The tracer (app/trace.py) already writes a full account of every retrieval and
generation to ``<CACHE>/runs/``. On a server that was a developer's tool; on a
desktop app it is the "full transparency" the product promises, sitting on the
user's own disk and reachable with no extra machinery. These endpoints expose it,
plus the log files, so the user never has to be told to go find a directory.

Read-only by design. Everything here hands back files the app itself wrote, and
every path is resolved and checked against its root before being opened — the run
name arrives from the client, so treating it as trusted would be a directory
traversal.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from app import logging_setup, paths
from app.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])

PROCESSES = ("backend", "worker")
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


def _safe(root: Path, *parts: str) -> Path:
    """Resolve ``parts`` under ``root``, refusing anything that escapes it."""
    root = root.resolve()
    target = root.joinpath(*parts).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid path")
    return target


@router.get("/paths")
async def get_paths(current_user: dict = Depends(get_current_user)) -> dict:
    """Where everything lives on this machine — so the UI can offer "open folder"
    and a support question can be answered without a screen share."""
    return {
        "data": str(paths.data_dir()),
        "cache": str(paths.cache_dir()),
        "logs": str(paths.log_dir()),
        "database": str(paths.db_path()),
        "documents": str(paths.documents_dir()),
        "runs": str(paths.runs_dir()),
        "log_files": {p: str(logging_setup.log_path(p)) for p in PROCESSES},
    }


@router.get("/logs/{process}")
async def get_log(process: str, lines: int = 200,
                  current_user: dict = Depends(get_current_user)) -> dict:
    """The tail of one process's log."""
    if process not in PROCESSES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown process: {process}")
    lines = max(1, min(lines, 5000))
    return {
        "process": process,
        "path": str(logging_setup.log_path(process)),
        "text": logging_setup.tail(process, lines),
    }


@router.get("/runs")
async def list_runs(limit: int = 50, current_user: dict = Depends(get_current_user)) -> dict:
    """Recent trace runs, newest first."""
    root = paths.runs_dir()
    entries = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            entries.append({"name": d.name, "modified": d.stat().st_mtime,
                            "files": sorted(f.name for f in d.iterdir() if f.is_file())})
        except OSError:
            continue
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return {"runs": entries[:max(1, min(limit, 500))], "root": str(root)}


@router.get("/runs/{name}/{filename}")
async def read_run_artifact(name: str, filename: str,
                            current_user: dict = Depends(get_current_user)) -> dict:
    """One artifact out of one run (the prompt, the retrieved context, ...)."""
    target = _safe(paths.runs_dir(), name, filename)
    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such artifact")
    if target.stat().st_size > MAX_ARTIFACT_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "artifact too large to display")
    return {"run": name, "file": filename,
            "text": target.read_text(encoding="utf-8", errors="replace")}


@router.delete("/runs", status_code=status.HTTP_200_OK)
async def prune_runs(keep: int = 50, current_user: dict = Depends(get_current_user)) -> dict:
    """Delete all but the ``keep`` most recent runs.

    Traces are cache, not data (see app/paths.py) — they accumulate one directory
    per chat turn and nothing else ever removes them.
    """
    import shutil

    root = paths.runs_dir()
    dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    removed = 0
    for d in dirs[max(0, keep):]:
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("diagnostics: pruned %d run director%s", removed, "y" if removed == 1 else "ies")
    return {"removed": removed, "kept": min(len(dirs), max(0, keep))}
