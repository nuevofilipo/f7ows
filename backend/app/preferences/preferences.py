"""
User preferences — the single source of truth for every user-tunable knob.

All the small inference-side settings that used to be hardcoded around the
codebase (chat model, temperature, max_tokens, retrieval budget, the system
prompt, ...) live here once: each with a default, a type, and a valid range.
Nothing else hardcodes these values; consumers call :func:`resolve` to get a
:class:`Preferences` for the current request.

Resolution is three layers, each overriding the previous:

    hardcoded DEFAULTS  ->  the user's stored settings (users.settings JSONB)
                        ->  per-request overrides (chat request body)

``resolve`` clamps everything to the schema's bounds, so a bad stored value or a
hostile request body can never produce an out-of-range setting. ``SCHEMA`` is the
machine-readable description of every field and drives both validation and the
settings UI (and doubles as the "transparent system prompt" surface).

Deliberately NOT here: ingestion-time knobs (chunk sizes, embedding model/dims,
cluster params). Those change stored artifacts and need re-ingestion to take
effect, so they stay global config, not live user settings.
"""
from __future__ import annotations

from dataclasses import dataclass, fields

from app.config import settings

# The default system prompt. Exposed as a setting so users can see and edit it.
DEFAULT_SYSTEM_PROMPT = (
    "You are a study assistant for STEM students. Answer the user's question using "
    "the sources provided below, which are numbered [S1], [S2], .... Cite each source "
    "you rely on inline using its number in brackets, e.g. [S1] or [S2], placed right "
    "after the claim it supports. If the sources do not contain the answer, say so "
    "plainly instead of guessing. Write all mathematics in KaTeX: inline as $...$ and "
    "display as $$...$$. Be precise and concise. "
    "Since the sources are oftentimes parsed PDFs the formatting might be off"
    "In that case use your prior knowledge on the concepts to fill in the gaps"
    "in order to provide great content to the user."
)


@dataclass(frozen=True)
class Preferences:
    chat_model: str
    temperature: float
    top_p: float
    top_k: int
    max_tokens: int
    system_prompt: str
    retrieval_budget: int
    retrieval_k: int
    planner_max_tokens: int
    default_intent: str


# Schema: one entry per Preferences field. ``default`` may be a literal or a
# zero-arg callable (used when the default comes from env config). num fields
# carry [min, max] clamps; str fields with ``choices`` are validated against them.
def _spec(typ, default, *, min=None, max=None, choices=None, description=""):
    return {"type": typ, "default": default, "min": min, "max": max,
            "choices": choices, "description": description}


SCHEMA: dict[str, dict] = {
    "chat_model": _spec(
        "str", lambda: settings.chat_model,
        description="OpenRouter model id used to write chat answers (BYOK)."),
    "temperature": _spec(
        "float", 1.0, min=0.0, max=2.0,
        description="Sampling temperature; higher = more creative."),
    "top_p": _spec(
        "float", 1.0, min=0.0, max=1.0,
        description="Nucleus sampling probability mass."),
    "top_k": _spec(
        "int", 0, min=0, max=500,
        description="Top-k sampling; 0 disables it."),
    "max_tokens": _spec(
        "int", 6000, min=1, max=10000,
        description="Maximum tokens in a chat answer."),
    "system_prompt": _spec(
        "str", DEFAULT_SYSTEM_PROMPT,
        description="System instructions prepended to every chat."),
    "retrieval_budget": _spec(
        "int", 15000, min=500, max=100000,
        description="Sparse-token budget for global-query retrieval."),
    "retrieval_k": _spec(
        "int", 8, min=1, max=50,
        description="Top-k chunks for specific (vector) retrieval."),
    "planner_max_tokens": _spec(
        "int", 2000, min=200, max=8000,
        description="Max tokens for the cheap planner LLM (skeleton branch selection "
                    "+ deepsummary outline). Reasoning models need headroom or they "
                    "spend the whole budget on reasoning and return empty."),
    "default_intent": _spec(
        "str", "auto", choices=("auto", "specific", "inbetween", "global"),
        description="Default retrieval intent when not set per message. 'auto' "
                    "classifies (keyword + LLM); 'inbetween' = focus on one topic "
                    "via skeleton; 'global' = whole project."),
}


def _default(key: str):
    d = SCHEMA[key]["default"]
    return d() if callable(d) else d


def _coerce(key: str, value):
    """Validate + clamp a single value against its schema spec. Returns the
    cleaned value, or raises ValueError on a wrong type / bad choice."""
    spec = SCHEMA[key]
    typ = spec["type"]
    if typ == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
    elif typ == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        value = float(value)
    elif typ == "str":
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if spec["choices"] is not None and value not in spec["choices"]:
            raise ValueError(f"{key} must be one of {spec['choices']}")
    if spec["min"] is not None:
        value = max(spec["min"], value)
    if spec["max"] is not None:
        value = min(spec["max"], value)
    return value


def validate(patch: dict) -> dict:
    """Clean a user-supplied settings patch for storage: drop unknown keys, type
    check + clamp known ones. Raises ValueError on a wrong type / bad choice."""
    cleaned = {}
    for key, value in patch.items():
        if key in SCHEMA:
            cleaned[key] = _coerce(key, value)
    return cleaned


def resolve(stored: dict | None, overrides: dict | None = None) -> Preferences:
    """Build the effective Preferences: DEFAULTS <- stored <- overrides, each
    value type-checked and clamped. Unknown or malformed entries are ignored so
    a stale/garbage stored blob can never break a request."""
    values = {}
    for f in fields(Preferences):
        values[f.name] = _default(f.name)
    for layer in (stored, overrides):
        if not layer:
            continue
        for key, value in layer.items():
            if key not in SCHEMA:
                continue
            try:
                values[key] = _coerce(key, value)
            except ValueError:
                pass  # ignore bad layered values; keep the prior (valid) one
    return Preferences(**values)


def describe() -> list[dict]:
    """Schema as a UI/transparency-friendly list (resolved defaults inlined)."""
    return [
        {"key": key, "type": spec["type"], "default": _default(key),
         "min": spec["min"], "max": spec["max"], "choices": list(spec["choices"]) if spec["choices"] else None,
         "description": spec["description"]}
        for key, spec in SCHEMA.items()
    ]
