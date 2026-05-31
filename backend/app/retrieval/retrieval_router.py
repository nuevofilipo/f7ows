r"""
The Dynamic Sparse Router (PHASE4_PLAN_v2 §4).

Global queries (``/deepsummary``, ``/mockexam``, "summarise everything") embed
poorly, so instead of one retrieval method we route by a **token budget** to the
cheapest strategy that still answers the question:

    intent "specific"                       -> 1. pgvector top-k over content_full
    intent "global", corpus <= budget       -> 2. brute stuff all content_sparse
    intent "global", corpus > budget        -> 3. agentic ltree skeleton (LLM plan)
    intent "global", corpus > budget, all   -> 4. medoid sampling (opt-in only)

Fidelity-first: over-budget global queries default to the skeleton (which returns
real, full sparse chunks of the relevant branches). Medoid sampling — the lossy
"one chunk per cluster" path — is never auto-selected; it only runs when the
caller explicitly asks for scope="all" (deliberate breadth over a massive corpus).

This module only *gathers* context (returns the assembled text + the chunks it
came from). It does not call the answering LLM — that's the chat layer's job.

The corpus size that drives the decision is estimated cheaply from
``SUM(length(content_sparse))`` (≈ chars/4 tokens) so we never load a huge corpus
just to decide we shouldn't load it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.db import acquire
from app.embed import embed

logger = logging.getLogger(__name__)

DEFAULT_BUDGET = 15_000   # sparse-token threshold between "stuff it" and routing
_CHARS_PER_TOKEN = 4      # rough estimate for the cheap corpus-size check -> could do this with tiktoken instead for accurate token counts

# Keyword sniffs for the "auto" intent heuristic — callers that know better (e.g.
# a slash command) should pass intent explicitly instead of relying on this.
_GLOBAL_HINTS = ("summar", "overview", "outline", "deepsummary", "mockexam",
                 "exam", "key points", "main points", "everything", "entire",
                 "whole", "all sources", "across all")


@dataclass
class RetrievalResult:
    strategy: str                       # "specific" | "stuff" | "skeleton" | "medoid"
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


# maybe this classification could be also llm-ized
def _classify_intent(query: str) -> str:
    q = query.lower()
    return "global" if any(h in q for h in _GLOBAL_HINTS) else "specific"


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


# ── strategy 4: medoid sampling ─────────────────────────────────────────────
async def _medoid(project_id, budget: int) -> RetrievalResult:
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ch.id, ch.path, ch.content_sparse, cl.label, cl.member_count
            FROM clusters cl JOIN chunks ch ON ch.id = cl.medoid_chunk_id
            WHERE cl.project_id = $1 AND cl.parent_id IS NULL
            ORDER BY cl.member_count DESC
            """,
            project_id,
        )
    context, chunks, total = _assemble(rows, budget=budget)
    for c, r in zip(chunks, rows):
        c["label"] = r["label"]
    return RetrievalResult("medoid", context, chunks, total, {"clusters": len(rows)})


# ── strategy 3: agentic ltree skeleton ──────────────────────────────────────
_PLANNER_SYSTEM = (
    "You are a retrieval planner. Given a table of contents (ltree paths) and a "
    "user question, choose the path prefixes whose branches are most relevant. "
    "Reply with ONLY a JSON array of path strings copied verbatim from the list, "
    "no prose."
)


async def _skeleton(project_id, query: str, budget: int) -> RetrievalResult:
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

    # Ask the cheap planner which branches matter; fall back to medoid on failure.
    try:
        reply = await complete(
            [
                {"role": "system", "content": _PLANNER_SYSTEM},
                {"role": "user", "content": f"Question: {query}\n\nPaths:\n" + "\n".join(toc)},
            ],
            max_tokens=400,
        )
        selected = [p for p in json.loads(reply[reply.index("["): reply.rindex("]") + 1]) if p in toc]
    except (LLMError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("skeleton planner failed (%s); falling back to medoid sampling", exc)
        result = await _medoid(project_id, budget)
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
    scope: str = "auto",
    k: int = 8,
    budget: int = DEFAULT_BUDGET,
) -> RetrievalResult:
    """Pick and run a retrieval strategy for ``query`` over ``project_id``.

    Args:
        intent: "specific" | "global" | "auto" (keyword heuristic).
        scope:  only "all" matters — it opts an over-budget global query into the
                lossy medoid sampler. Anything else (incl. the default) uses the
                skeleton, which keeps full chunks.
        k:      top-k for the specific strategy.
        budget: sparse-token threshold; also caps how much context is assembled.

    Returns:
        A RetrievalResult with the assembled context and the chunks it used.
    """
    if intent == "auto":
        intent = _classify_intent(query)
    if intent == "specific":
        return await _specific(project_id, query, k)

    # Global: estimate corpus size cheaply to choose between stuffing and routing.
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

    if corpus_tokens <= budget:
        # Fits the budget -> highest fidelity: stuff every chunk's sparse text.
        result = await _stuff(project_id, budget)
    elif scope == "all":
        # Opt-in only: deliberate breadth-over-depth (one medoid per cluster) for a
        # massive corpus. Never auto-selected — it's the lossiest path.
        result = await _medoid(project_id, budget)
    else:
        # Default over-budget path: planner selects the relevant branches and we
        # stuff their full sparse content to the budget — real chunks, not samples.
        # For a broad query the planner just selects everything, degrading
        # gracefully to a budget-capped stuff.
        result = await _skeleton(project_id, query, budget)

    result.meta["corpus_tokens"] = corpus_tokens
    result.meta["intent"] = intent
    return result
