"""
Chat commands — single-pass formatting commands like /mockexam.

A command is a small bundle: a *preprompt* (extra instructions appended to the
system prompt), optional retrieval overrides, and an optional *parser* that turns
the model's answer into structured metadata. send_message just looks one up,
appends its instructions, and runs its parser — the heavy strings/logic live here
so chat.py stays thin.

Only single-pass commands belong here. Multi-step agentic flows (e.g.
/deepsummary: recursively break topics -> generate per subtopic -> recombine)
are a separate feature, not a formatting preprompt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from app.chat import deepsummary


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    instructions: str                          # appended to the system prompt
    intent: str | None = None                  # retrieval intent override (None = auto)
    # Keyword appended to the *routing* query so the command always trips the
    # non-specific keyword pass and reaches the LLM intent classifier (-> inbetween
    # or global, never a specific lookup). The answer is still grounded on the real
    # query, so this never pollutes the generated text.
    retrieval_keyword: str | None = None
    parse: Callable[[str], dict] | None = None  # answer text -> structured metadata
    # Active-recall commands (mock exam, later voice recall) exercise memory, so the
    # chunks they cite are marked "actively_studied"; everything else is passive
    # review ("passively_studied"). See app.chat.study.
    active_recall: bool = False
    # Background commands (e.g. deepsummary) are multi-pass: send_message inserts a
    # placeholder assistant message and spawns ``runner`` instead of replying inline.
    background: bool = False
    runner: Callable | None = None             # async (project_id, topic, prefs, message_id, document_ids)


# ── /mockexam ───────────────────────────────────────────────────────────────
# Strict triple-marker template so the answer parses unambiguously and stays
# LaTeX-safe (the ASCII markers never collide with $...$ or markdown).
_MOCKEXAM_INSTRUCTIONS = (
    "The user wants a mock exam generated from the sources. Produce a set of exam "
    "questions (default 5 unless the user asks for a specific number), mixing "
    "multiple-choice and open questions. Output EACH question in exactly this "
    "format, with nothing else between questions:\n"
    "<<<Q>>>\n"
    "the question text, including any multiple-choice options as (A) (B) (C) (D)\n"
    "<<<A>>>\n"
    "the full worked answer (the correct option and why), citing sources as [S#]\n"
    "<<<END>>>\n"
    "Do not reveal answers inside the <<<Q>>> section. Keep all mathematics in KaTeX."
)

_EXAM_RE = re.compile(r"<<<Q>>>(.*?)<<<A>>>(.*?)<<<END>>>", re.DOTALL)


def _parse_mockexam(answer: str) -> dict:
    """Pull {question, answer} pairs out of the triple-marker template so the
    frontend can render questions and hide each answer behind a toggle."""
    items = [
        {"question": q.strip(), "answer": a.strip()}
        for q, a in _EXAM_RE.findall(answer)
    ]
    return {"exam": items}


COMMANDS: dict[str, Command] = {
    "mockexam": Command(
        name="mockexam",
        description="Generate a mock exam (question/answer pairs) from the sources.",
        instructions=_MOCKEXAM_INSTRUCTIONS,
        # intent stays auto: a mock exam may be about one topic or the whole course;
        # the keyword just guarantees we don't treat it as a precise lookup.
        retrieval_keyword="mockexam",
        parse=_parse_mockexam,
        active_recall=True,  # a mock exam exercises memory -> actively_studied
    ),
    "deepsummary": Command(
        name="deepsummary",
        description="Generate a long, structured deep-dive on a topic. Multi-pass; "
                    "the reply fills in asynchronously (poll the message until ready).",
        instructions=deepsummary.INSTRUCTIONS,
        retrieval_keyword="deepsummary",
        background=True,
        runner=deepsummary.run,
    ),
}


def get(name: str | None) -> Command | None:
    return COMMANDS.get(name) if name else None


def describe() -> list[dict]:
    """Commands as a transparency-friendly list (name + description + preprompt)."""
    return [
        {"name": c.name, "description": c.description, "instructions": c.instructions,
         "intent": c.intent, "retrieval_keyword": c.retrieval_keyword, "background": c.background}
        for c in COMMANDS.values()
    ]
