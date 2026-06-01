"""Testsuite configuration — fixed local endpoint, namespaced test identity, paths.

Everything the driver creates lives under a dedicated test user + project so the
real data is never touched; `python -m testsuite clean` wipes it.
"""
from __future__ import annotations

from pathlib import Path

# backend/ (testsuite/config.py -> testsuite -> backend)
BACKEND_DIR = Path(__file__).resolve().parent.parent
PDF_DIR = BACKEND_DIR / "testsuite" / "pdfs_to_upload"
RUNS_DIR = BACKEND_DIR / "runs"

# Local server the driver spawns and talks to.
HOST = "127.0.0.1"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}"

# Namespaced test identity (created on first run, reused thereafter).
TEST_EMAIL = "testsuite@local.test"
TEST_PASSWORD = "testsuite-password"
TEST_PROJECT_NAME = "testsuite"
