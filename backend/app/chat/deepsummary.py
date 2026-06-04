"""
deepsummary — the multi-pass "deep dive" command.

Not a single-pass formatting command: it (1) outlines the topic into subtopics
with a cheap planner call, (2) generates a detailed, cited section per subtopic
(concurrently), and (3) assembles them into one long document.

It's I/O-bound (a handful of LLM calls), so send_message runs it as an in-process
asyncio task — no ARQ worker. The result is written back into the placeholder
assistant message (content = the document, metadata.state = "ready").

Citation note: each section's [S#] markers number that section's OWN retrieved
sources, so per-section cited lists are stored in metadata.sections; the markers
stay meaningful within each section (rendered with its own sources).
"""
from __future__ import annotations

import asyncio
import json
import logging

from app import cost, trace
from app.chat.generate import cited_chunks, grounded_answer
from app.chat.study import PASSIVELY_STUDIED, mark_studied
from app.config import settings
from app.db import acquire
from app.preferences import Preferences

logger = logging.getLogger(__name__)

_MAX_SECTIONS = 7
_SECTION_CONCURRENCY = 4

_OUTLINE_PROMPT = (
    "You are planning a deep, structured explanation built from the sources. Break "
    "the user's topic into 3-7 focused, non-overlapping subtopics that together "
    "give a complete deep dive. Reply with ONLY a JSON array of short subtopic "
    "title strings, nothing else."
)
_SECTION_PROMPT = (
    "You are writing one section of a deep-dive study document for a STEM student. "
    "Explain the given subtopic thoroughly and self-containedly, grounded in the "
    "sources and citing them inline as [S#]. Write mathematics in KaTeX ($...$ / "
    "$$...$$). Do not add a heading; start directly with the explanation."
)

# Surfaced by GET /commands for transparency (the pipeline's prompts).
INSTRUCTIONS = f"Multi-pass deep dive.\n\nOutline prompt:\n{_OUTLINE_PROMPT}\n\nSection prompt:\n{_SECTION_PROMPT}"


async def _outline(project_id, topic: str, prefs: Preferences, document_ids=None) -> list[str]:
    """Decompose the topic into subtopics (cheap planner call -> JSON array)."""
    ga = await grounded_answer(project_id, topic, prefs, system_prompt=_OUTLINE_PROMPT,
                               routing_query=f"{topic} deepsummary",
                               model=settings.planner_model,
                               max_tokens=prefs.planner_max_tokens,
                               document_ids=document_ids)
    try:
        arr = json.loads(ga.answer[ga.answer.index("["): ga.answer.rindex("]") + 1])
        subs = [str(s).strip() for s in arr if str(s).strip()]
    except (ValueError, json.JSONDecodeError):
        subs = []
    return subs[:_MAX_SECTIONS] or [topic]


# i think this would very much benefit from agentic but stuffy retrieval
async def _section(project_id, subtopic: str, prefs: Preferences, sem: asyncio.Semaphore,
                   document_ids=None):
    async with sem:
        ga = await grounded_answer(project_id, subtopic, prefs, system_prompt=_SECTION_PROMPT,
                                   routing_query=f"{subtopic} deepsummary",
                                   document_ids=document_ids)
    return subtopic, ga


async def _set_result(message_id, content: str, metadata: dict) -> None:
    async with acquire() as conn:
        await conn.execute(
            "UPDATE messages SET content = $2, metadata = $3::jsonb WHERE id = $1",
            message_id, content, json.dumps(metadata),
        )


async def run(project_id, topic: str, prefs: Preferences, message_id, document_ids=None) -> None:
    """Run the pipeline and write the result into the placeholder message."""
    logger.info("deepsummary: starting %s for topic %r", message_id, topic)
    # This task runs in its own copied context; zero the accumulator so it sums
    # only this pipeline's calls (outline + every section).
    cost.reset()
    try:
        with trace.begin_run("deepsummary", label=topic,
                             project_id=str(project_id), message_id=str(message_id)) as tr:
            tr.write("preferences.md", trace.render_prefs(prefs, topic=topic))
            outline = await _outline(project_id, topic, prefs, document_ids)
            tr.write("pass1_outline.md", f"# Outline: {topic}\n\n" +
                     "\n".join(f"{i + 1}. {s}" for i, s in enumerate(outline)))
            sem = asyncio.Semaphore(_SECTION_CONCURRENCY)
            pairs = await asyncio.gather(
                *[_section(project_id, s, prefs, sem, document_ids) for s in outline])

            document = f"# {topic}\n\n" + "\n\n".join(f"## {sub}\n\n{ga.answer}" for sub, ga in pairs)

            sections_meta, union, seen = [], [], set()
            for i, (sub, ga) in enumerate(pairs, 1):
                cited = cited_chunks(ga.answer, ga.retrieval.chunks)
                tr.write(f"pass2_{i:02d}_{trace._slug(sub)}.md", f"## {sub}\n\n{ga.answer}")
                sections_meta.append({"title": sub, "cited": cited})
                for c in cited:
                    if c["id"] not in seen:
                        union.append(c)
                        seen.add(c["id"])
            tr.write("final.md", document)

            await _set_result(message_id, document, {
                "state": "ready", "command": "deepsummary", "topic": topic,
                "outline": outline, "sections": sections_meta, "cited": union,
                "cost": cost.total(),
            })
            # A deep summary is passive review -> mark every cited chunk studied.
            await mark_studied(project_id, [c["id"] for c in union], PASSIVELY_STUDIED)
            logger.info("deepsummary: %s ready (%d sections)", message_id, len(outline))
    except Exception as exc:  # noqa: BLE001 - record on the message, never crash the loop
        logger.exception("deepsummary failed: %s", message_id)
        await _set_result(message_id, f"Deep summary failed: {exc}",
                          {"state": "error", "command": "deepsummary", "error": str(exc)})
