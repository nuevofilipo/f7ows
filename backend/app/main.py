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
from app.db import close_pool, create_pool
from app.producer import close_redis_pool, create_redis_pool
from app.library.documents import router as documents_router
from app.library.projects import router as projects_router
from app.preferences.api import router as settings_router
from app.projections import router as projection_router


@asynccontextmanager
# question when is this closed? on every request
async def lifespan(app: FastAPI):
    await create_pool()
    await create_redis_pool()
    sweeper = asyncio.create_task(run_sweeper())  # marks stuck "generating" msgs as error
    yield
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper
    await close_redis_pool()
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


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
