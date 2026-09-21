"""Testsuite configuration — fixed local endpoint, namespaced test identity, paths.

Everything the driver creates lives under a dedicated test user + project so the
real data is never touched; `python -m testsuite clean` wipes it.
"""
from __future__ import annotations

import os
from pathlib import Path

from app import paths

# backend/ (testsuite/config.py -> testsuite -> backend)
BACKEND_DIR = Path(__file__).resolve().parent.parent
PDF_DIR = BACKEND_DIR / "testsuite" / "pdfs_to_upload"
# Traces now live in the app cache dir, not backend/runs — same resolution the
# server uses (app/trace.py), so the driver reads the runs it just produced.
RUNS_DIR = Path(os.environ["TRACE_DIR"]).expanduser() if os.getenv("TRACE_DIR") else paths.runs_dir()

# Local server the driver spawns and talks to.
HOST = "127.0.0.1"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}"

# Namespaced test identity (created on first run, reused thereafter).
TEST_EMAIL = "testsuite@local.test"
TEST_PASSWORD = "testsuite-password"
TEST_PROJECT_NAME = "testsuite"
