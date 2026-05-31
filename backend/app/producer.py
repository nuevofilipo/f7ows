"""
Job queue — the *producer* side of ARQ (used by the FastAPI web process).

The web app doesn't run heavy work itself; it pushes a job onto Redis and returns.
A separate worker process (`arq app.worker.WorkerSettings`) consumes the queue and
executes the job functions defined in app/worker.py. This module just owns the
Redis connection the web app enqueues through — mirroring db.py's pool pattern.
"""
from __future__ import annotations

import logging
from uuid import UUID

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import settings

logger = logging.getLogger(__name__)

_redis: ArqRedis | None = None


async def create_redis_pool() -> ArqRedis:
    global _redis
    _redis = await create_pool(RedisSettings.from_dsn(settings.redis_public_url))
    logger.info("producer: ARQ redis pool created")
    return _redis


async def close_redis_pool() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def get_redis() -> ArqRedis:
    if _redis is None:
        raise RuntimeError("Redis pool is not initialised. Call create_redis_pool() first.")
    return _redis

# Actual Enqueuing functions / endpoints start here

async def enqueue_ingest(document_id: UUID | str) -> None:
    """Queue the ingest pipeline for a freshly-uploaded document."""
    job = await get_redis().enqueue_job("ingest_document", str(document_id))
    # enqueue_job returns None if a job with the same id is already queued.
    logger.info("producer: enqueued ingest_document(%s) job_id=%s",
                document_id, getattr(job, "job_id", None))


async def enqueue_projections(project_id: UUID | str) -> None:
    """Queue a project-wide projection recompute (after a document is ingested)."""
    job = await get_redis().enqueue_job("recalculate_projections", str(project_id))
    logger.info("producer: enqueued recalculate_projections(%s) job_id=%s",
                project_id, getattr(job, "job_id", None))


async def enqueue_clusters(project_id: UUID | str) -> None:
    """Queue a project-wide cluster recompute (after a document is ingested)."""
    job = await get_redis().enqueue_job("recalculate_clusters", str(project_id))
    logger.info("producer: enqueued recalculate_clusters(%s) job_id=%s",
                project_id, getattr(job, "job_id", None))

# this one is enqueued once all 
async def enqueue_finalize(project_id: UUID | str, document_ids: list) -> None:
    """Queue a single post-upload finalize that recomputes projections + clusters
    once, after all of ``document_ids`` have finished ingesting."""
    ids = [str(d) for d in document_ids]
    job = await get_redis().enqueue_job("finalize_project", str(project_id), ids)
    logger.info("producer: enqueued finalize_project(%s, %d docs) job_id=%s",
                project_id, len(ids), getattr(job, "job_id", None))
