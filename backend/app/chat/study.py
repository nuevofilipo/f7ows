"""
Study tracking — record how chunks have been studied so the map can colour them.

Every grounded answer cites a few chunks. *How* they were cited tells us how the
material was studied: an active-recall command (a mock exam, later voice recall)
exercises memory, while ordinary chat or a summary is passive review. We collapse
that into one of three states stored on ``chunks.study_status`` (JSON ``{"state":
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

from app.db import acquire, placeholders

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
            # The project scope is a subquery rather than Postgres' UPDATE..FROM:
            # SQLite supports UPDATE..FROM too, but the join form cannot be
            # combined with an expanded IN list as legibly, and this reads closer
            # to the sentence "these chunks, if they belong to this project".
            cursor = await conn.execute(
                f"""
                UPDATE chunks
                SET study_status = json_object('state', ?)
                WHERE id IN ({placeholders(ids)})
                  AND document_id IN (SELECT id FROM documents WHERE project_id = ?)
                  AND ? > CASE json_extract(study_status, '$.state')
                        WHEN 'actively_studied'  THEN 2
                        WHEN 'passively_studied' THEN 1
                        ELSE 0
                      END
                """,
                state, *ids, project_id, rank,
            )
        return cursor.rowcount
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
        # "has a state" rather than a literal <> '{}' comparison: study_status is
        # TEXT, so two JSON objects that are equal can still differ as strings
        # (key order, whitespace). Asking json1 whether the key is present is the
        # comparison we actually mean.
        cursor = await conn.execute(
            """
            UPDATE chunks
            SET study_status = '{}'
            WHERE document_id IN (SELECT id FROM documents WHERE project_id = ?)
              AND json_extract(study_status, '$.state') IS NOT NULL
            """,
            project_id,
        )
    return cursor.rowcount
