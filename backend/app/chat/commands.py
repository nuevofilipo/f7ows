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
    intent: str | None = None                  # retrieval intent override
    scope: str | None = None                   # retrieval scope override
    parse: Callable[[str], dict] | None = None  # answer text -> structured metadata
    # Background commands (e.g. deepsummary) are multi-pass: send_message inserts a
    # placeholder assistant message and spawns ``runner`` instead of replying inline.
    background: bool = False
    runner: Callable | None = None             # async (project_id, topic, prefs, message_id)


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
        intent="global",   # an exam should cover the material broadly
        parse=_parse_mockexam,
    ),
    "deepsummary": Command(
        name="deepsummary",
        description="Generate a long, structured deep-dive on a topic. Multi-pass; "
                    "the reply fills in asynchronously (poll the message until ready).",
        instructions=deepsummary.INSTRUCTIONS,
        intent="global",
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
         "intent": c.intent, "scope": c.scope, "background": c.background}
        for c in COMMANDS.values()
    ]
