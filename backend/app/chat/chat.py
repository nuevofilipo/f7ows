"""
Chat API — the core "talk to your sources" feature.

A *chat* is a tree of messages sharing a ``chat_id`` in the `messages` table; the
dotted ``path`` column gives reddit-style nested threading (``0001.0002``, see
:func:`_next_child_path`). Sending a message:

  1. inserts the user message (under an explicit ``parent_path``, or linearly
     under the chat's latest message),
  2. gathers the conversation history = that message's ancestor branch,
  3. routes retrieval (:func:`app.retrieval.route`) to ground the answer in the
     project's sources,
  4. calls the chat model (:func:`app.llm.complete`),
  5. stores the assistant reply as a child message (with citations in metadata),
  6. returns it.

Non-streaming for now — the whole answer comes back in the response. Streaming
(SSE) is a planned follow-up.
"""
from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app import cost, trace
from app.auth import get_current_user
from app.chat import commands
from app.chat.generate import cited_chunks, grounded_answer
from app.chat.study import ACTIVELY_STUDIED, PASSIVELY_STUDIED, mark_studied
from app.db import acquire, ancestors_of, placeholders
from app.llm import LLMError
from app.preferences import resolve

# Strong refs to in-flight background tasks (deepsummary etc.) so they aren't
# garbage-collected mid-run; discarded on completion.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

router = APIRouter(prefix="/projects/{project_id}/chats", tags=["chat"])

# Commands are global (not project-scoped), so they get their own router.
commands_router = APIRouter(prefix="/commands", tags=["chat"])


@commands_router.get("")
async def list_commands(current_user: dict = Depends(get_current_user)) -> list[dict]:
    """Available chat commands and their preprompts (transparency)."""
    return commands.describe()


def _message_dict(row) -> dict:
    meta = row["metadata"]
    return {
        "id": row["id"],
        "chat_id": row["chat_id"],
        "path": str(row["path"]),
        "role": row["role"],
        "content": row["content"],
        "metadata": json.loads(meta) if isinstance(meta, str) else (meta or {}),
        "created_at": row["created_at"],
    }


async def _owned_project(project_id: UUID, user_id) -> None:
    """404 unless the project exists and belongs to the current user."""
    async with acquire() as conn:
        owner = await conn.fetchval(
            "SELECT 1 FROM projects WHERE id = ? AND user_id = ?", project_id, user_id,
        )
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")


async def _validate_source_ids(project_id: UUID, raw) -> list[UUID] | None:
    """Coerce a body ``source_ids`` list to project-owned document UUIDs.

    Returns the validated ids, or None when none are given/valid (= whole project).
    Silently drops malformed or foreign ids so a stale selection can't 422 a send.
    """
    if not isinstance(raw, list) or not raw:
        return None
    parsed = []
    for x in raw:
        try:
            parsed.append(UUID(str(x)))
        except (ValueError, TypeError):
            continue
    if not parsed:
        return None
    async with acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id FROM documents WHERE project_id = ? "
            f"AND id IN ({placeholders(parsed)})",
            project_id, *parsed,
        )
    valid = [r["id"] for r in rows]
    return valid or None


# Sibling labels are zero-padded to this width. Both ltree and TEXT order labels
# lexicographically, so an unpadded '10' sorts before '2' — which scrambled
# `ORDER BY path` (and therefore the whole rendered thread) as soon as any level
# reached ten siblings. Four digits is a thousand more replies than a branch will
# ever hold; past 9999 the ordering degrades but nothing breaks.
_LABEL_WIDTH = 4


async def _next_child_path(conn, chat_id: UUID, parent_path: str | None) -> str:
    """The next sibling path under ``parent_path`` (or at the chat root)."""
    if parent_path is None:
        # Root-level messages are the ones with a single label, i.e. no dot.
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND instr(path, '.') = 0",
            chat_id,
        )
        return f"{n + 1:0{_LABEL_WIDTH}d}"
    # Direct children only: below `parent_path`, and with nothing after their own
    # label — substr() past the parent and its dot is that label, so it must
    # contain no further dot. (GLOB, not LIKE: see app.db.descendants_of.)
    n = await conn.fetchval(
        """
        SELECT COUNT(*) FROM messages
        WHERE chat_id = ?
          AND path GLOB ? || '.*'
          AND instr(substr(path, length(?) + 2), '.') = 0
        """,
        chat_id, parent_path, parent_path,
    )
    return f"{parent_path}.{n + 1:0{_LABEL_WIDTH}d}"


async def _insert_message(conn, project_id, chat_id, path, role, content, metadata) -> dict:
    row = await conn.fetchrow(
        """
        INSERT INTO messages (id, project_id, chat_id, path, role, content, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id, chat_id, path, role, content, metadata, created_at
        """,
        uuid4(), project_id, chat_id, path, role, content, json.dumps(metadata),
    )
    return _message_dict(row)

# OUTWARD POINTING ENDPOINTS STARTING FROM HERE:

@router.post("/{chat_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(
    project_id: UUID,
    chat_id: UUID,
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Post a message and get a grounded assistant reply.

    Body: ``{content, parent_path?, intent?, scope?, source_ids?}``. Omit
    ``parent_path`` to continue the chat linearly; pass one to branch a nested
    reply. ``intent`` / ``scope`` override the retrieval router heuristics (see
    app.retrieval.route). ``source_ids`` scopes retrieval to the user's selected
    documents (omit/empty = the whole project).
    """
    await _owned_project(project_id, current_user["id"])
    body = await request.json()
    content = str(body.get("content", "")).strip()
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Message content is required")

    # Optional command (e.g. "mockexam") — injects a formatting preprompt, may
    # override retrieval intent, and post-parses the answer.
    command_name = body.get("command")
    command = commands.get(command_name)
    if command_name and command is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown command: {command_name}")

    # Effective settings: stored user prefs, with optional per-request overrides
    # from a "settings" patch in the body. intent may also be set directly;
    # precedence is body override > command override > user default.
    overrides = body.get("settings") if isinstance(body.get("settings"), dict) else None
    prefs = resolve(current_user["settings"], overrides)
    intent = body.get("intent") or (command.intent if command else None) or prefs.default_intent
    # Commands inject a keyword into the routing query so they reach the intent
    # classifier (the answer is still grounded on the real content).
    routing_query = (f"{content} {command.retrieval_keyword}"
                     if command and command.retrieval_keyword else None)

    # Source scope: the documents the user has selected in the left panel. Validate
    # the ids belong to this project, then pass them down to scope retrieval. None
    # (no/invalid ids) means "the whole project" — the frontend blocks sending with
    # an empty selection, so a scoped request always carries at least one valid id.
    document_ids = await _validate_source_ids(project_id, body.get("source_ids"))

    # Visual hierarchy level (1=main thread, deeper on reply, capped at 3). The
    # frontend computes it from the two send buttons; we persist it on both the
    # user and assistant turn so the tree survives a reload.
    try:
        level = int(body.get("level", 1))
    except (TypeError, ValueError):
        level = 1
    level = max(1, min(3, level))

    # Insert the user turn and read back its branch (the conversation history).
    async with acquire() as conn:
        parent_path = body.get("parent_path")
        if parent_path is None:
            parent_path = await conn.fetchval(
                "SELECT path FROM messages WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1",
                chat_id,
            )
        history = []
        if parent_path is not None:
            # The conversation history is the branch from the root down to the
            # parent, i.e. every message whose path is an ancestor of (or equal
            # to) it — Postgres' `path @> $2`.
            branch, branch_args = ancestors_of("path", str(parent_path))
            history = await conn.fetch(
                f"""
                SELECT role, content FROM messages
                WHERE chat_id = ? AND {branch} AND role IN ('user', 'assistant')
                ORDER BY path
                """,
                chat_id, *branch_args,
            )
        user_path = await _next_child_path(conn, chat_id, parent_path and str(parent_path))
        user_msg = await _insert_message(
            conn, project_id, chat_id, user_path, "user", content,
            {**({"command": command.name} if command else {}), "level": level},
        )

    # Background command (e.g. deepsummary): insert a placeholder assistant message,
    # kick off the multi-pass pipeline as an in-process task, and return at once.
    # The task fills the message in later (poll until metadata.state == "ready").
    if command and command.background:
        async with acquire() as conn:
            assistant_path = await _next_child_path(conn, chat_id, user_path)
            assistant_msg = await _insert_message(
                conn, project_id, chat_id, assistant_path, "assistant", "",
                {"state": "generating", "command": command.name, "level": level},
            )
        _spawn(command.runner(project_id, content, prefs, assistant_msg["id"], document_ids))
        return {"user_message": user_msg, "assistant_message": assistant_msg}

    # Synchronous path: retrieve + generate one grounded answer.
    system_prompt = prefs.system_prompt + (f"\n\n{command.instructions}" if command else "")
    # Zero the cost accumulator so it captures exactly this turn's model calls
    # (the planner inside retrieval + the answer); read back into metadata below.
    cost.reset()
    with trace.begin_run("chat", label=command.name if command else "msg",
                         project_id=str(project_id), chat_id=str(chat_id)) as tr:
        tr.write("preferences.md", trace.render_prefs(prefs, intent=intent,
                                                      command=command.name if command else None))
        try:
            result = await grounded_answer(project_id, content, prefs, system_prompt=system_prompt,
                                           history=history, intent=intent, routing_query=routing_query,
                                           document_ids=document_ids)
        except LLMError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Chat model failed: {exc}")
        answer, retrieval = result.answer, result.retrieval

        # retrieved = everything we put in front of the model; cited = the subset it
        # actually referenced via [S#] markers (parsed from the answer).
        retrieved = [{"id": c["id"], "path": c["path"]} for c in retrieval.chunks]
        cited = cited_chunks(answer, retrieval.chunks)
        # Command-specific structured output (e.g. the parsed mock-exam questions).
        extra = command.parse(answer) if command and command.parse else {}
        # cost is None when no provider in this turn reported a price (a local
        # model), which the UI shows as "no cost" rather than "$0.00" — the two
        # are not the same claim. See app/cost.py.
        spend = cost.summary()
        metadata = {"strategy": retrieval.strategy, "retrieval_tokens": retrieval.token_count,
                    "retrieved": retrieved, "cited": cited, "level": level,
                    "cost": spend["usd"], "unpriced_calls": spend["unpriced_calls"],
                    "source_ids": [str(d) for d in document_ids] if document_ids else None, **extra}
        if command:
            metadata["command"] = command.name
        tr.set(strategy=retrieval.strategy, retrieval_tokens=retrieval.token_count,
               cited=[c["path"] for c in cited])
        tr.write("answer.md", f"# Answer\n\n{answer}\n\n---\n\n"
                 f"**strategy**: `{retrieval.strategy}`  "
                 f"**retrieved**: {len(retrieved)}  **cited**: {len(cited)}\n\n"
                 + "\n".join(f"- [S?] cited `{c['path']}`" for c in cited))
        async with acquire() as conn:
            assistant_path = await _next_child_path(conn, chat_id, user_path)
            assistant_msg = await _insert_message(
                conn, project_id, chat_id, assistant_path, "assistant", answer, metadata,
            )
        # Record how the cited chunks were studied for the projection map: an
        # active-recall command (mock exam) marks them actively_studied, plain chat
        # passively_studied. Monotonic + best-effort (never breaks the reply).
        study_state = ACTIVELY_STUDIED if (command and command.active_recall) else PASSIVELY_STUDIED
        await mark_studied(project_id, [c["id"] for c in cited], study_state)
        return {"user_message": user_msg, "assistant_message": assistant_msg,
                "cited": cited, "retrieved": retrieved, **extra}


@router.get("/{chat_id}/messages")
async def list_messages(
    project_id: UUID,
    chat_id: UUID,
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """All messages in a chat, in tree order (by path)."""
    await _owned_project(project_id, current_user["id"])
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, chat_id, path, role, content, metadata, created_at
            FROM messages WHERE project_id = ? AND chat_id = ?
            ORDER BY path
            """,
            project_id, chat_id,
        )
    return [_message_dict(r) for r in rows]


@router.get("")
async def list_chats(
    project_id: UUID,
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """Chats in a project, most recently active first."""
    await _owned_project(project_id, current_user["id"])
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT chat_id, COUNT(*) AS message_count, MAX(created_at) AS last_activity
            FROM messages WHERE project_id = ?
            GROUP BY chat_id ORDER BY last_activity DESC
            """,
            project_id,
        )
    return [dict(r) for r in rows]
