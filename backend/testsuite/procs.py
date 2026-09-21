"""Spawn & tear down the two backend processes (uvicorn web + the worker).

The driver speaks only HTTP to the web server; the worker drains the `jobs` table
in the background so the real ingest pipeline actually runs. Both inherit TRACE=1
so the embedded tracer writes runs/.

Everything is local now — the database is a file, and there is no queue server —
so this needs nothing running beforehand.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx

from testsuite.config import BACKEND_DIR, BASE_URL, HOST, PORT


def _spawn(args: list[str], env: dict) -> subprocess.Popen:
    return subprocess.Popen(args, cwd=str(BACKEND_DIR), env=env)


def _wait_healthy(timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if httpx.get(f"{BASE_URL}/healthz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError as exc:  # not up yet
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"web server did not become healthy in {timeout}s (last: {last})")


@contextmanager
def managed_backend(*, quiet_worker: bool = False):
    """Start uvicorn + the worker, wait for /healthz, yield, then tear both down."""
    env = {**os.environ, "TRACE": "1"}
    web = _spawn([sys.executable, "-m", "uvicorn", "app.main:app",
                  "--host", HOST, "--port", str(PORT), "--log-level", "warning"], env)
    worker_out = subprocess.DEVNULL if quiet_worker else None
    worker = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=str(BACKEND_DIR), env=env, stdout=worker_out, stderr=worker_out,
    )
    try:
        _wait_healthy()
        print(f"[procs] web + worker up (web pid={web.pid}, worker pid={worker.pid})")
        yield
    finally:
        for name, p in (("worker", worker), ("web", web)):
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        print("[procs] web + worker stopped")
