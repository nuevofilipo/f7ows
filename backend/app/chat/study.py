"""
Study tracking — record how chunks have been studied so the map can colour them.

Every grounded answer cites a few chunks. *How* they were cited tells us how the
material was studied: an active-recall command (a mock exam, later voice recall)
exercises memory, while ordinary chat or a summary is passive review. We collapse
that into one of three states stored on ``chunks.study_status`` (JSONB ``{"state":
...}``), which the projection map reads back to colour each point:

    actively_studied   — exercised via an active-recall command (filled accent)
    passively_studied  — read via plain chat / summary           (accent outline)
    not_studied        — never cited (the default ``{}``)          (paper outline)

The single writer, :func:`mark_studied`, is **monotonic**: it only ever raises a
chunk's state, never lowers it — so a chunk you actively recalled stays
"actively_studied" even if it later turns up in a passive answer.
"""
from __future__ import annotations

import logging
from uuid import UUID

from app.db import acquire

logger = logging.getLogger(__name__)

# The three study states, ordered weakest -> strongest. ``not_studied`` is the
# implicit default (no row written), so it never needs to be set explicitly.
NOT_STUDIED = "not_studied"
PASSIVELY_STUDIED = "passively_studied"
ACTIVELY_STUDIED = "actively_studied"

# Rank used for the monotonic upgrade. The SQL mirrors this CASE for the *current*
# state, so the two must stay in sync.
_RANK = {NOT_STUDIED: 0, PASSIVELY_STUDIED: 1, ACTIVELY_STUDIED: 2}


async def mark_studied(project_id, chunk_ids, state: str) -> int:
    """Raise the study state of ``chunk_ids`` (within ``project_id``) to ``state``.

    Monotonic: a chunk is only updated when ``state`` outranks its current state,
    so passive review can't overwrite an earlier active recall. Returns the number
    of chunks actually upgraded. A failure here is logged, not raised — study
    tracking must never break a chat reply.
    """
    rank = _RANK.get(state)
    if rank is None:
        raise ValueError(f"unknown study state: {state!r}")
    ids = [UUID(str(c)) for c in chunk_ids]
    if rank == 0 or not ids:
        return 0  # nothing to do (no-op state or no chunks)

    try:
        async with acquire() as conn:
            result = await conn.execute(
                """
                UPDATE chunks c
                SET study_status = jsonb_build_object('state', $1::text)
                FROM documents d
                WHERE c.document_id = d.id
                  AND d.project_id = $2
                  AND c.id = ANY($3::uuid[])
                  AND $4::int > CASE c.study_status->>'state'
                        WHEN 'actively_studied'  THEN 2
                        WHEN 'passively_studied' THEN 1
                        ELSE 0
                      END
                """,
                state, project_id, ids, rank,
            )
        # asyncpg returns e.g. "UPDATE 3"; pull the row count off the tag.
        return int(result.split()[-1]) if result else 0
    except Exception:  # noqa: BLE001 - never let study tracking break a reply
        logger.warning("mark_studied failed for project %s", project_id, exc_info=True)
        return 0


async def clear_studied(project_id) -> int:
    """Reset every chunk in ``project_id`` back to ``not_studied``.

    The deliberate inverse of :func:`mark_studied`'s monotonic upgrade — a manual
    "start over" for the projection map, clearing all active *and* passive states
    in one shot. Writes the default ``{}`` (which reads back as ``not_studied``)
    rather than a ``{"state": ...}`` blob. Returns the number of chunks cleared.
    """
    async with acquire() as conn:
        result = await conn.execute(
            """
            UPDATE chunks c
            SET study_status = '{}'::jsonb
            FROM documents d
            WHERE c.document_id = d.id
              AND d.project_id = $1
              AND c.study_status <> '{}'::jsonb
            """,
            project_id,
        )
    return int(result.split()[-1]) if result else 0
