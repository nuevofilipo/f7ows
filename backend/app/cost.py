"""
Per-context cost accumulator — sums the USD cost of every model call made in the
active context (mirrors :mod:`app.trace`).

OpenRouter returns the real dollar cost of each generation in ``usage.cost`` when
the request asks for it (``usage: {include: true}``). :func:`app.llm.complete`
adds each call's cost here, so a single chat turn — which may fan out into a cheap
planner call plus the answer (and, for deepsummary, several section calls) — is
summed automatically without threading a cost argument through every function.

**Unpriced calls are counted, not assumed free.** A local Ollama reports no cost
at all, and so does any gateway that doesn't implement the field. Summing those
as zero would show "$0.00" — indistinguishable from a call that really was free,
and wrong the moment one turn mixes a local planner with a paid chat model. So a
call that reports nothing increments a separate counter, and :func:`summary`
returns ``usd=None`` when *nothing* in the turn was priced, which the UI renders
as "local model, no cost".

The chat handler :func:`reset` s the accumulator at the start of a turn and reads
it afterwards to store on the assistant message. Because the values live in
:class:`contextvars.ContextVar` s, they are isolated per request and copied into
any ``asyncio.create_task`` spawned during it (e.g. background deepsummary), so a
background task accumulates independently of its parent.
"""
from __future__ import annotations

from contextvars import ContextVar

_total: ContextVar[float] = ContextVar("model_cost_usd", default=0.0)
_priced: ContextVar[int] = ContextVar("model_calls_priced", default=0)
_unpriced: ContextVar[int] = ContextVar("model_calls_unpriced", default=0)


def add(amount: float | None) -> None:
    """Record one model call. ``None`` (or a missing field) means the provider
    reported no cost — counted as unpriced rather than added as zero."""
    if amount is None:
        _unpriced.set(_unpriced.get() + 1)
        return
    _total.set(_total.get() + amount)
    _priced.set(_priced.get() + 1)


def total() -> float:
    """The accumulated cost (USD) for the active context. Unpriced calls
    contribute nothing — use :func:`summary` to tell "free" from "unknown"."""
    return _total.get()


def summary() -> dict:
    """``{usd, priced_calls, unpriced_calls}`` for the active context.

    ``usd`` is None when no call in the turn reported a price, which is the
    signal to display "no cost" instead of "$0.00".
    """
    priced, unpriced = _priced.get(), _unpriced.get()
    return {
        "usd": _total.get() if priced else None,
        "priced_calls": priced,
        "unpriced_calls": unpriced,
    }


def reset() -> None:
    """Zero the accumulator for the active context (call at the start of a turn)."""
    _total.set(0.0)
    _priced.set(0)
    _unpriced.set(0)
