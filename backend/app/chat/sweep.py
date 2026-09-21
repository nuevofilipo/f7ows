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
import time

from app.db import acquire

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 600       # 10 min — comfortably longer than any real run
SWEEP_INTERVAL_SECONDS = 120


async def sweep_once(*, startup: bool = False) -> int:
    """Mark messages stuck in 'generating' as errored.

    ``startup=True`` drops the age test. Those placeholders are written by
    in-process tasks of *this* process, so at startup every one of them belongs
    to a process that no longer exists — waiting out a 10-minute threshold to say
    so just makes the user stare at a spinner for a crash that already happened.
    """
    age_clause = "" if startup else (
        "  AND created_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ? || ' seconds')"
    )
    args = () if startup else (-STALE_AFTER_SECONDS,)
    async with acquire() as conn:
        # json_patch is json1's merge: it returns the left object with the right
        # object's keys overlaid, which is what Postgres' `||` did for jsonb.
        # The age test is a plain string comparison — safe because created_at and
        # NOW share one fixed-width UTC format (see app/migrations.NOW), so
        # lexical order is chronological order.
        cursor = await conn.execute(
            f"""
            UPDATE messages
            SET metadata = json_patch(
                    metadata,
                    '{{"state":"error","error":"generation interrupted (stale)"}}')
            WHERE json_extract(metadata, '$.state') = 'generating'
            {age_clause}
            """,
            *args,
        )
    n = cursor.rowcount
    if n:
        logger.info("sweep: marked %d stale 'generating' message(s) as error", n)
    return n


def _slept(wall_delta: float, mono_delta: float) -> bool:
    """Whether the machine suspended during the last interval.

    ``time.monotonic`` does not advance while the host is suspended but wall time
    does, so a large gap between the two is the signal. One sweep interval of
    slack absorbs ordinary scheduling jitter.
    """
    return wall_delta - mono_delta > SWEEP_INTERVAL_SECONDS


async def run_sweeper() -> None:
    """Sweep on startup, then every SWEEP_INTERVAL_SECONDS, until cancelled.

    Skips exactly one cycle after the machine wakes from sleep. This matters far
    more on a laptop than it ever did on a server: closing the lid mid-generation
    freezes the process, and on wake the wall clock has jumped past the staleness
    threshold even though the task is alive and about to resume. Sweeping then
    would mark healthy work as failed. One skipped cycle gives it a chance to
    finish; if it really did die, the next cycle still catches it.
    """
    try:
        await sweep_once(startup=True)
    except Exception:  # noqa: BLE001 - a transient failure must not kill the loop
        logger.exception("sweep: startup sweep failed")

    last_wall, last_mono = time.time(), time.monotonic()
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        wall, mono = time.time(), time.monotonic()
        wall_delta, mono_delta = wall - last_wall, mono - last_mono
        last_wall, last_mono = wall, mono
        if _slept(wall_delta, mono_delta):
            logger.info("sweep: host slept ~%ds; skipping one cycle so in-flight "
                        "work isn't marked stale on wake", int(wall_delta - mono_delta))
            continue
        try:
            await sweep_once()
        except Exception:  # noqa: BLE001 - a transient failure must not kill the loop
            logger.exception("sweep: sweep_once failed")
