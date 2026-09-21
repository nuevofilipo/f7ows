"""
The single grounded-answer primitive, shared by the chat endpoint and multi-pass
commands (e.g. deepsummary).

``grounded_answer`` does one thing: retrieve sources for a query and ask the LLM,
returning the answer plus the RetrievalResult (so callers can attribute the [S#]
citations). No HTTP, no DB, no message persistence — those stay in the callers,
so this can be looped N times by a pipeline without going through the endpoint.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app import trace
from app.llm import complete
from app.preferences import Preferences
from app.retrieval import RetrievalResult, route


@dataclass
class GroundedAnswer:
    answer: str
    retrieval: RetrievalResult


# Inline citation markers the model emits, e.g. "[S2]" -> source #2.
_CITE_RE = re.compile(r"\[S(\d+)\]")


def cited_chunks(answer: str, chunks: list[dict]) -> list[dict]:
    """Map the [S#] markers the model wrote back to the chunks it actually used.

    Each entry carries ``s`` (the original marker number, so the frontend can map
    an inline ``[S#]`` straight to its chunk) plus the chunk ``text`` (whichever
    content field the strategy populated), which drives the hover-to-peek popup.
    """
    cited, seen = [], set()
    for num in sorted(int(n) for n in _CITE_RE.findall(answer)):
        if 1 <= num <= len(chunks):
            c = chunks[num - 1]
            if c["id"] not in seen:
                cited.append({"s": num, "id": c["id"], "path": c["path"],
                              "text": c.get("content_full") or c.get("content_sparse") or ""})
                seen.add(c["id"])
    return cited


async def grounded_answer(
    project_id,
    query: str,
    prefs: Preferences,
    *,
    system_prompt: str,
    history=(),
    intent: str = "auto",
    routing_query: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    document_ids=None,
) -> GroundedAnswer:
    """Retrieve sources for ``query`` and generate one grounded answer.

    Messages are ordered system -> history -> (sources + question) so the static
    instructions and prior turns form a cache-friendly prefix. ``model`` /
    ``max_tokens`` override the prefs (e.g. a cheap planner model for an outline).
    ``routing_query`` (if given) is what's sent to the retrieval router for
    intent classification + planning, while the answer is still grounded on the
    real ``query`` — slash commands use this to inject their keyword so they reach
    the LLM classifier instead of being mis-read as a specific lookup.
    ``document_ids`` scopes retrieval to the user's selected sources (None = all).
    """
    retrieval = await route(project_id, routing_query or query, intent=intent,
                            k=prefs.retrieval_k, budget=prefs.retrieval_budget,
                            planner_max_tokens=prefs.planner_max_tokens,
                            document_ids=document_ids)
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": h["role"], "content": h["content"]} for h in history]
    messages.append({"role": "user", "content": f"# Sources\n{retrieval.context}\n\n# Question\n{query}"})
    trace.current_run().write("prompt.md", trace.render_messages(messages), unique=True)
    answer = await complete(
        messages, model=model or prefs.chat_model, max_tokens=max_tokens or prefs.max_tokens,
        temperature=prefs.temperature, top_p=prefs.top_p, top_k=prefs.top_k,
        role="chat",
    )
    return GroundedAnswer(answer, retrieval)
