r"""
The Dynamic Sparse Router (PHASE4_PLAN_v2 §4).

Global queries (``/deepsummary``, ``/mockexam``, "summarise everything") embed
poorly, so instead of one retrieval method we route by **intent** — a three-level
distinction — to the strategy that answers the question without over-fetching:

    intent "specific"                  -> pgvector top-k over content_full
    intent "inbetween"                 -> agentic ltree skeleton (LLM picks the
                                          relevant branches; ANY corpus size)
    intent "global", corpus <= budget  -> brute stuff all content_sparse
    intent "global", corpus  > budget  -> skeleton (degrades to a budget-capped
                                          stuff when the planner selects the whole tree)

The middle level matters: "summarise topic X" needs *all of one topic*, not the
whole project, so it must focus (skeleton) even on a small corpus — never blindly
stuff everything. Raw stuffing is reserved for a genuinely global query that fits
the budget. The medoid sampler (one chunk per cluster) was lossy and is gone;
clusters are still computed for the map UI.

Intent classification (``intent="auto"``): a free keyword pass decides
specific-vs-not; only when it trips do we spend one cheap LLM call to split
"inbetween" from "global" (fallback: inbetween).

This module only *gathers* context (returns the assembled text + the chunks it
came from). It does not call the answering LLM — that's the chat layer's job.

The corpus size that drives the stuff-vs-skeleton choice for a global query is
estimated cheaply from ``SUM(length(content_sparse))`` (≈ chars/4 tokens) so we
never load a huge corpus just to decide we shouldn't load it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app import trace
from app.db import acquire
from app.embed import embed

logger = logging.getLogger(__name__)

DEFAULT_BUDGET = 15_000   # sparse-token threshold between "stuff it" and routing
_CHARS_PER_TOKEN = 4      # rough estimate for the cheap corpus-size check -> could do this with tiktoken instead for accurate token counts

# Keyword sniffs for the cheap "specific vs not" pass. A hit only means "not a
# precise lookup" — the inbetween/global split is then made by the LLM. Slash
# commands inject their keyword into the routing query so they always trip this.
_GLOBAL_HINTS = ("summar", "overview", "outline", "deepsummary", "mockexam",
                 "exam", "key points", "main points", "everything", "entire",
                 "whole", "all sources", "across all")


@dataclass
class RetrievalResult:
    strategy: str                       # "specific" | "stuff" | "skeleton"
    context: str                        # assembled text to feed the answering LLM
    chunks: list[dict] = field(default_factory=list)  # sources used (path + text)
    token_count: int = 0                # estimated tokens in context
    meta: dict = field(default_factory=dict)


# ── token counting (local, lazy tiktoken) ──────────────────────────────────
_enc = None


def _count(text: str) -> int:
    global _enc
    if _enc is None:
        try:
            import tiktoken

            _enc = tiktoken.get_encoding("o200k_base")
        except Exception:  # noqa: BLE001
            _enc = False
    return len(_enc.encode(text)) if _enc else max(1, len(text) // _CHARS_PER_TOKEN)


def _is_specific(query: str) -> bool:
    """Cheap keyword pass: True unless the query trips a non-specific hint."""
    q = query.lower()
    return not any(h in q for h in _GLOBAL_HINTS)


_SCOPE_SYSTEM = (
    "Classify the user's question into exactly one word:\n"
    "- \"inbetween\": about ONE topic or section — it needs everything on that "
    "topic, but not the whole body of material.\n"
    "- \"global\": spans the ENTIRE body of material (an overview/summary of "
    "everything, across all sources).\n"
    "Reply with ONLY that one word, nothing else."
)


async def _llm_classify_scope(query: str, planner_max_tokens: int) -> str:
    """Split a non-specific query into "inbetween" vs "global" with one cheap
    planner call. Falls back to "inbetween" (the focused, safe default) on any
    failure or an unrecognised reply."""
    from app.llm import LLMError, complete

    try:
        reply = await complete(
            [{"role": "system", "content": _SCOPE_SYSTEM},
             {"role": "user", "content": query}],
            max_tokens=planner_max_tokens,
        )
    except LLMError as exc:
        logger.warning("scope classifier failed (%s); defaulting to inbetween", exc)
        return "inbetween"
    return "global" if "global" in reply.lower() else "inbetween"


def _number_context(chunks: list[dict], text_key: str) -> str:
    """Format chunks as numbered source blocks ``[S1] (path)\\n<text>`` so the model
    can cite them inline as [S1], [S2], ... and we can map those numbers back to
    chunk ids (chunks[i] is source S{i+1})."""
    return "\n\n".join(
        f"[S{i + 1}] ({c['path']})\n{c.get(text_key, '')}" for i, c in enumerate(chunks)
    )


def _assemble(rows, *, budget: int) -> tuple[str, list[dict], int]:
    """Build numbered ``content_sparse`` source blocks, stopping once the running
    token estimate would exceed ``budget`` (always keeping at least one block)."""
    chunks, blocks, total = [], [], 0
    for r in rows:
        text = r["content_sparse"] or ""
        block = f"[S{len(chunks) + 1}] ({r['path']})\n{text}"
        cost = _count(block)
        if total + cost > budget and chunks:
            break
        chunks.append({"id": str(r["id"]), "path": str(r["path"]), "content_sparse": text})
        blocks.append(block)
        total += cost
    return "\n\n".join(blocks), chunks, total


# ── strategy 1: specific lookup ─────────────────────────────────────────────
async def _specific(project_id, query: str, k: int) -> RetrievalResult:
    [vector] = await embed([query])
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.path, c.content_full, (c.embedding <=> $2::halfvec) AS distance
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE d.project_id = $1 AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> $2::halfvec
            LIMIT $3
            """,
            project_id, vector, k,
        )
    chunks = [{"id": str(r["id"]), "path": str(r["path"]), "content_full": r["content_full"],
               "distance": float(r["distance"])} for r in rows]
    context = _number_context(chunks, "content_full")
    return RetrievalResult("specific", context, chunks, _count(context), {"k": k})


# ── strategy 2: brute-force stuffing ────────────────────────────────────────
async def _stuff(project_id, budget: int) -> RetrievalResult:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.path, c.content_sparse
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE d.project_id = $1 AND c.content_sparse IS NOT NULL
            ORDER BY c.path
            """,
            project_id,
        )
    context, chunks, total = _assemble(rows, budget=budget)
    return RetrievalResult("stuff", context, chunks, total, {"available": len(rows)})


# ── strategy 3: agentic ltree skeleton ──────────────────────────────────────
_PLANNER_SYSTEM = (
    "You are a retrieval planner. Given a table of contents (ltree paths) and a "
    "user question, choose the path prefixes whose branches are most relevant. "
    "Reply with ONLY a JSON array of path strings copied verbatim from the list, "
    "no prose."
    "This decides which content gets passed to a RAG system, so with global queries prefer"
    "selecting as many relevant path prefixes as possible"
)


async def _skeleton(project_id, query: str, budget: int, planner_max_tokens: int) -> RetrievalResult:
    from app.llm import LLMError, complete

    async with acquire() as conn:
        toc = [str(r["path"]) for r in await conn.fetch(
            """
            SELECT DISTINCT c.path
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE d.project_id = $1 AND c.path IS NOT NULL
            ORDER BY c.path
            """,
            project_id,
        )]
    if not toc:
        return RetrievalResult("skeleton", "", [], 0, {"reason": "empty toc"})

    # Ask the cheap planner which branches matter; fall back to a budget-capped
    # stuff of the whole corpus on failure.
    try:
        reply = await complete(
            [
                {"role": "system", "content": _PLANNER_SYSTEM},
                {"role": "user", "content": f"Question: {query}\n\nPaths:\n" + "\n".join(toc)},
            ],
            max_tokens=planner_max_tokens,
        )
        selected = [p for p in json.loads(reply[reply.index("["): reply.rindex("]") + 1]) if p in toc]
    except (LLMError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("skeleton planner failed (%s); falling back to budget-capped stuff", exc)
        result = await _stuff(project_id, budget)
        result.meta["fellback_from"] = "skeleton"
        return result

    if not selected:
        selected = toc  # planner found nothing specific -> consider the whole tree

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, c.path, c.content_sparse
            FROM chunks c JOIN documents d ON d.id = c.document_id
            WHERE d.project_id = $1 AND c.path <@ ANY($2::ltree[])
            ORDER BY c.path
            """,
            project_id, selected,
        )
    context, chunks, total = _assemble(rows, budget=budget)
    return RetrievalResult("skeleton", context, chunks, total,
                           {"selected_paths": selected, "toc_size": len(toc)})


# ── public entry point ──────────────────────────────────────────────────────
async def route(
    project_id,
    query: str,
    *,
    intent: str = "auto",
    k: int = 8,
    budget: int = DEFAULT_BUDGET,
    planner_max_tokens: int = 2000,
) -> RetrievalResult:
    """Pick and run a retrieval strategy for ``query`` over ``project_id``.

    Args:
        intent: "specific" | "inbetween" | "global" | "auto". "auto" = free keyword
                pass (specific vs not), then one cheap LLM call to split the rest
                into inbetween vs global.
        k:      top-k for the specific strategy.
        budget: sparse-token threshold (stuff vs skeleton for global); also caps
                how much context is assembled.
        planner_max_tokens: max tokens for the planner / classifier LLM calls.

    Returns:
        A RetrievalResult with the assembled context and the chunks it used.
    """
    if intent == "auto":
        intent = "specific" if _is_specific(query) else await _llm_classify_scope(query, planner_max_tokens)

    if intent == "specific":
        result = await _specific(project_id, query, k)
    elif intent == "inbetween":
        # Focus on the relevant topic, regardless of corpus size — never stuff the
        # whole project for a topic question.
        result = await _skeleton(project_id, query, budget, planner_max_tokens)
    else:  # global
        # Estimate corpus size cheaply: if the whole thing fits, stuff it (highest
        # fidelity); otherwise the skeleton selects branches and caps to budget,
        # degrading to a budget-capped stuff when the planner picks everything.
        async with acquire() as conn:
            chars = await conn.fetchval(
                """
                SELECT COALESCE(SUM(length(c.content_sparse)), 0)
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE d.project_id = $1
                """,
                project_id,
            )
        corpus_tokens = int(chars) // _CHARS_PER_TOKEN
        result = (await _stuff(project_id, budget) if corpus_tokens <= budget
                  else await _skeleton(project_id, query, budget, planner_max_tokens))
        result.meta["corpus_tokens"] = corpus_tokens

    result.meta["intent"] = intent
    trace.current_run().write(
        "retrieval.md", _render_retrieval(query, intent, budget, result), unique=True
    )
    return result


def _render_retrieval(query: str, intent: str, budget: int, result) -> str:
    """The router's decision + the exact context it assembled (transparency)."""
    paths = "\n".join(f"  - [S{i + 1}] `{c['path']}`" for i, c in enumerate(result.chunks))
    return (
        f"# Retrieval\n\n"
        f"- **query**: {query!r}\n"
        f"- **intent**: `{intent}`  **budget**: {budget}\n"
        f"- **strategy chosen**: `{result.strategy}`\n"
        f"- **context tokens**: {result.token_count}\n"
        f"- **meta**: `{result.meta}`\n\n"
        f"## sources ({len(result.chunks)})\n{paths}\n\n"
        f"## assembled context (verbatim, fed to the LLM)\n\n```\n{result.context}\n```\n"
    )
