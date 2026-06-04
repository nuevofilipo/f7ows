"""
Per-context cost accumulator — sums the USD cost of every model call made in the
active context (mirrors :mod:`app.apikey` and :mod:`app.trace`).

OpenRouter returns the real dollar cost of each generation in ``usage.cost`` when
the request asks for it (``usage: {include: true}``). :func:`app.llm.complete`
adds each call's cost here, so a single chat turn — which may fan out into a cheap
planner call plus the answer (and, for deepsummary, several section calls) — is
summed automatically without threading a cost argument through every function.

The chat handler :func:`reset` s the accumulator at the start of a turn and reads
:func:`total` afterwards to store on the assistant message. Because the value
lives in a :class:`contextvars.ContextVar`, it is isolated per request and copied
into any ``asyncio.create_task`` spawned during it (e.g. background deepsummary),
so a background task accumulates independently of its parent.
"""
from __future__ import annotations

from contextvars import ContextVar

_total: ContextVar[float] = ContextVar("model_cost_usd", default=0.0)


def add(amount: float | None) -> None:
    """Accumulate the cost (USD) of one model call into the active context."""
    if amount:
        _total.set(_total.get() + amount)


def total() -> float:
    """The accumulated cost (USD) for the active context."""
    return _total.get()


def reset() -> None:
    """Zero the accumulator for the active context (call at the start of a turn)."""
    _total.set(0.0)
