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
    """Map the [S#] markers the model wrote back to the chunks it actually used."""
    cited, seen = [], set()
    for num in sorted(int(n) for n in _CITE_RE.findall(answer)):
        if 1 <= num <= len(chunks):
            c = chunks[num - 1]
            if c["id"] not in seen:
                cited.append({"id": c["id"], "path": c["path"]})
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
    scope: str = "auto",
    model: str | None = None,
    max_tokens: int | None = None,
) -> GroundedAnswer:
    """Retrieve sources for ``query`` and generate one grounded answer.

    Messages are ordered system -> history -> (sources + question) so the static
    instructions and prior turns form a cache-friendly prefix. ``model`` /
    ``max_tokens`` override the prefs (e.g. a cheap planner model for an outline).
    """
    retrieval = await route(project_id, query, intent=intent, scope=scope,
                            k=prefs.retrieval_k, budget=prefs.retrieval_budget)
    messages = [{"role": "system", "content": system_prompt}]
    messages += [{"role": h["role"], "content": h["content"]} for h in history]
    messages.append({"role": "user", "content": f"# Sources\n{retrieval.context}\n\n# Question\n{query}"})
    answer = await complete(
        messages, model=model or prefs.chat_model, max_tokens=max_tokens or prefs.max_tokens,
        temperature=prefs.temperature, top_p=prefs.top_p, top_k=prefs.top_k,
    )
    return GroundedAnswer(answer, retrieval)
