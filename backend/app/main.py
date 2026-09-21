"""
FastAPI application entry point.
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import router as auth_router
from app.chat import commands_router, router as chat_router
from app.chat.sweep import run_sweeper
from app.config import settings
from app.diagnostics import router as diagnostics_router
from app.db import close_pool, create_pool
from app.logging_setup import setup as setup_logging
from app.local_user import ensure_local_user
from app.library.documents import router as documents_router
from app.library.projects import router as projects_router
from app.preferences.api import router as settings_router
from app.projections import router as projection_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown for the whole process — not per request.

    The body before `yield` runs once as the app boots, the body after it runs
    once as it shuts down; `yield` is where the app serves traffic.

    There is no queue connection to open any more: jobs are rows in the same
    SQLite database (app/producer.py), so create_pool() covers both.
    """
    # A file log before anything else: the shell owns this process's stdout,
    # so an early failure would otherwise leave nothing to look at.
    setup_logging("backend")
    await create_pool()
    # Seed the single local user before serving: every request resolves to it
    # (auth.get_current_user), so it has to exist before the first one arrives.
    await ensure_local_user()
    sweeper = asyncio.create_task(run_sweeper())  # marks stuck "generating" msgs as error
    yield
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper
    await close_pool()


app = FastAPI(
    title="NotebookLM Alternative API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — cookies require allow_credentials=True and an explicit (non-"*") origin list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(documents_router)
app.include_router(chat_router)
app.include_router(commands_router)
app.include_router(settings_router)
app.include_router(projection_router)
app.include_router(diagnostics_router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
