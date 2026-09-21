"""
Job queue — the *producer* side (used by the FastAPI web process).

The web app doesn't run heavy work itself; it records a job and returns. A
separate worker process (``python -m app.worker``) claims jobs and executes the
functions defined in app/worker.py.

The queue is a table in the same SQLite database as everything else (`jobs`, see
app/migrations.py), not Redis. For a single-user desktop app that is a strictly
better trade: no second server to install, supervise, or explain to the user, and
the queue lands inside the one file that "back up your data" already covers. What
it costs is push notification — Redis could block until a job arrived, whereas
the worker has to poll. At a human-scale job rate that is a sub-second delay
nobody can perceive, against a whole daemon removed from the install.

Enqueueing deliberately does **not** open its own connection: it borrows the web
process's, so a job is inserted with the same `acquire()` discipline as any other
write and is visible to the worker as soon as the transaction commits.

The public functions keep the names and signatures they had under ARQ, so the
routers that call them did not change.
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

from app.db import NOW, acquire

logger = logging.getLogger(__name__)


async def enqueue(func: str, *args, delay_seconds: float = 0) -> int:
    """Record one job and return its id.

    ``args`` are stored as a JSON array and handed back to the worker function
    positionally, so they have to be JSON-serialisable — UUIDs are stringified
    here rather than at every call site.

    ``delay_seconds`` sets ``run_after``, which is what replaces ARQ's
    ``_defer_by``: the worker only claims rows whose ``run_after`` has passed.
    """
    payload = json.dumps([str(a) if isinstance(a, UUID) else a for a in args])
    # `?` cannot appear inside strftime's modifier list, so the offset is
    # concatenated as a string ('+5.0 seconds'), the same shape app/chat/sweep.py
    # uses for its backdating.
    run_after = NOW if not delay_seconds else (
        f"strftime('%Y-%m-%dT%H:%M:%fZ','now','+{float(delay_seconds)} seconds')")
    async with acquire() as conn:
        cursor = await conn.execute(
            f"INSERT INTO jobs (func, args, run_after) VALUES (?, ?, {run_after})",
            func, payload,
        )
    job_id = cursor.lastrowid
    logger.info("producer: queued %s(%s) job_id=%s", func, ", ".join(map(str, args)), job_id)
    return job_id


# Actual Enqueuing functions / endpoints start here

async def enqueue_ingest(document_id: UUID | str) -> None:
    """Queue the ingest pipeline for a freshly-uploaded document."""
    await enqueue("ingest_document", document_id)


async def enqueue_projections(project_id: UUID | str) -> None:
    """Queue a project-wide projection recompute (after a document is ingested)."""
    await enqueue("recalculate_projections", project_id)


async def enqueue_clusters(project_id: UUID | str) -> None:
    """Queue a project-wide cluster recompute (after a document is ingested)."""
    await enqueue("recalculate_clusters", project_id)


# this one is enqueued once all
async def enqueue_finalize(project_id: UUID | str, document_ids: list,
                           attempts_left: int | None = None,
                           delay_seconds: float = 0) -> None:
    """Queue a single post-upload finalize that recomputes projections + clusters
    once, after all of ``document_ids`` have finished ingesting.

    ``attempts_left`` and ``delay_seconds`` are how the job re-queues *itself*
    while it waits (see app.worker.finalize_project); ordinary callers pass
    neither.
    """
    ids = [str(d) for d in document_ids]
    if attempts_left is None:
        await enqueue("finalize_project", project_id, ids)
    else:
        await enqueue("finalize_project", project_id, ids, attempts_left,
                      delay_seconds=delay_seconds)
