"""
Stale background-task sweep.

In-process background commands (deepsummary) write a placeholder message with
metadata.state = "generating" and fill it in when done. If the web process
restarts (or the task dies) mid-run, that message would stay "generating"
forever. This periodic sweep marks such messages "error" once they're older than
a threshold, so the UI stops waiting. It also runs once on startup, cleaning up
anything left over from a previous crash.
"""
from __future__ import annotations

import asyncio
import logging

from app.db import acquire

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 600       # 10 min — comfortably longer than any real run
SWEEP_INTERVAL_SECONDS = 120


async def sweep_once() -> int:
    """Mark messages stuck in 'generating' past the threshold as errored."""
    async with acquire() as conn:
        result = await conn.execute(
            """
            UPDATE messages
            SET metadata = metadata || '{"state":"error","error":"generation interrupted (stale)"}'::jsonb
            WHERE metadata->>'state' = 'generating'
              AND created_at < NOW() - make_interval(secs => $1)
            """,
            STALE_AFTER_SECONDS,
        )
    n = int(result.split()[-1]) if result else 0
    if n:
        logger.info("sweep: marked %d stale 'generating' message(s) as error", n)
    return n


async def run_sweeper() -> None:
    """Sweep on startup, then every SWEEP_INTERVAL_SECONDS, until cancelled."""
    while True:
        try:
            await sweep_once()
        except Exception:  # noqa: BLE001 - a transient failure must not kill the loop
            logger.exception("sweep: sweep_once failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
