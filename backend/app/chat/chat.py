"""
Chat API — the core "talk to your sources" feature.

A *chat* is a tree of messages sharing a ``chat_id`` in the `messages` table; the
``path`` ltree column gives reddit-style nested threading for free. Sending a
message:

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
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import get_current_user
from app.chat import commands
from app.chat.generate import cited_chunks, grounded_answer
from app.db import acquire
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
            "SELECT 1 FROM projects WHERE id = $1 AND user_id = $2", project_id, user_id,
        )
    if owner is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")


async def _next_child_path(conn, chat_id: UUID, parent_path: str | None) -> str:
    """The next sibling ltree path under ``parent_path`` (or at the chat root)."""
    if parent_path is None:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM messages WHERE chat_id = $1 AND nlevel(path) = 1", chat_id,
        )
        return str(n + 1)
    n = await conn.fetchval(
        """
        SELECT COUNT(*) FROM messages
        WHERE chat_id = $1 AND path <@ $2::ltree AND nlevel(path) = nlevel($2::ltree) + 1
        """,
        chat_id, parent_path,
    )
    return f"{parent_path}.{n + 1}"


async def _insert_message(conn, project_id, chat_id, path, role, content, metadata) -> dict:
    row = await conn.fetchrow(
        """
        INSERT INTO messages (project_id, chat_id, path, role, content, metadata)
        VALUES ($1, $2, $3::ltree, $4, $5, $6::jsonb)
        RETURNING id, chat_id, path, role, content, metadata, created_at
        """,
        project_id, chat_id, path, role, content, json.dumps(metadata),
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

    Body: ``{content, parent_path?, intent?, scope?}``. Omit ``parent_path`` to
    continue the chat linearly; pass one to branch a nested reply. ``intent`` /
    ``scope`` override the retrieval router heuristics (see app.retrieval.route).
    """
    await _owned_project(project_id, current_user["id"])
    body = await request.json()
    content = str(body.get("content", "")).strip()
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Message content is required")

    # Optional command (e.g. "mockexam") — injects a formatting preprompt and may
    # override retrieval intent/scope and post-parse the answer.
    command_name = body.get("command")
    command = commands.get(command_name)
    if command_name and command is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown command: {command_name}")

    # Effective settings: stored user prefs, with optional per-request overrides
    # from a "settings" patch in the body. intent/scope may also be set directly;
    # precedence is body override > command override > user default.
    overrides = body.get("settings") if isinstance(body.get("settings"), dict) else None
    prefs = resolve(current_user["settings"], overrides)
    intent = body.get("intent") or (command.intent if command else None) or prefs.default_intent
    scope = body.get("scope") or (command.scope if command else None) or prefs.default_scope

    # Insert the user turn and read back its branch (the conversation history).
    async with acquire() as conn:
        parent_path = body.get("parent_path")
        if parent_path is None:
            parent_path = await conn.fetchval(
                "SELECT path FROM messages WHERE chat_id = $1 ORDER BY created_at DESC LIMIT 1",
                chat_id,
            )
        history = []
        if parent_path is not None:
            history = await conn.fetch(
                """
                SELECT role, content FROM messages
                WHERE chat_id = $1 AND path @> $2::ltree AND role IN ('user', 'assistant')
                ORDER BY path
                """,
                chat_id, str(parent_path),
            )
        user_path = await _next_child_path(conn, chat_id, parent_path and str(parent_path))
        user_msg = await _insert_message(
            conn, project_id, chat_id, user_path, "user", content,
            {"command": command.name} if command else {},
        )

    # Background command (e.g. deepsummary): insert a placeholder assistant message,
    # kick off the multi-pass pipeline as an in-process task, and return at once.
    # The task fills the message in later (poll until metadata.state == "ready").
    if command and command.background:
        async with acquire() as conn:
            assistant_path = await _next_child_path(conn, chat_id, user_path)
            assistant_msg = await _insert_message(
                conn, project_id, chat_id, assistant_path, "assistant", "",
                {"state": "generating", "command": command.name},
            )
        _spawn(command.runner(project_id, content, prefs, assistant_msg["id"]))
        return {"user_message": user_msg, "assistant_message": assistant_msg}

    # Synchronous path: retrieve + generate one grounded answer.
    system_prompt = prefs.system_prompt + (f"\n\n{command.instructions}" if command else "")
    try:
        result = await grounded_answer(project_id, content, prefs, system_prompt=system_prompt,
                                       history=history, intent=intent, scope=scope)
    except LLMError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Chat model failed: {exc}")
    answer, retrieval = result.answer, result.retrieval

    # retrieved = everything we put in front of the model; cited = the subset it
    # actually referenced via [S#] markers (parsed from the answer).
    retrieved = [{"id": c["id"], "path": c["path"]} for c in retrieval.chunks]
    cited = cited_chunks(answer, retrieval.chunks)
    # Command-specific structured output (e.g. the parsed mock-exam questions).
    extra = command.parse(answer) if command and command.parse else {}
    metadata = {"strategy": retrieval.strategy, "retrieval_tokens": retrieval.token_count,
                "retrieved": retrieved, "cited": cited, **extra}
    if command:
        metadata["command"] = command.name
    async with acquire() as conn:
        assistant_path = await _next_child_path(conn, chat_id, user_path)
        assistant_msg = await _insert_message(
            conn, project_id, chat_id, assistant_path, "assistant", answer, metadata,
        )
    return {"user_message": user_msg, "assistant_message": assistant_msg,
            "cited": cited, "retrieved": retrieved, **extra}


@router.get("/{chat_id}/messages")
async def list_messages(
    project_id: UUID,
    chat_id: UUID,
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """All messages in a chat, in tree order (ltree path)."""
    await _owned_project(project_id, current_user["id"])
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, chat_id, path, role, content, metadata, created_at
            FROM messages WHERE project_id = $1 AND chat_id = $2
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
            FROM messages WHERE project_id = $1
            GROUP BY chat_id ORDER BY last_activity DESC
            """,
            project_id,
        )
    return [dict(r) for r in rows]
