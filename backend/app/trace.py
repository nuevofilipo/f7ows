"""
Transparent execution tracing.

A context-scoped :class:`Tracer` that the real backend code taps into, so that
*any* invocation — driven by the testsuite or by a real frontend request —
automatically drops human-readable artifacts into ``<CACHE>/runs/runN_<kind>_<label>/``.

Design:

- **Zero footprint when off.** Tracing is enabled with ``TRACE=1`` (default on in
  dev). When disabled, or when no run is active, :func:`current_run` returns a
  no-op tracer, so every tap site (``tr.write(...)``, ``tr.timer(...)``) is a
  harmless call. The deep functions never change their signatures.
- **No signature threading.** The active run lives in a :class:`contextvars.ContextVar`,
  so ``route()`` / ``embed()`` / ``grounded_answer()`` just ask "is a run active?"
  and write if so. contextvars are per-task, so concurrent chats don't collide,
  and ``asyncio.create_task`` inherits the context at spawn time.
- **Cross-process safe run ids.** Run directories are created with an exclusive
  ``mkdir`` against a filesystem counter, so the web process and the ARQ worker
  can both write into ``runs/`` without coordinating.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from app import paths

logger = logging.getLogger(__name__)

def _runs_dir() -> Path:
    """Where run directories go: the app cache dir (``<CACHE>/runs``), or
    ``TRACE_DIR`` when set.

    Resolved per call rather than at import: ``paths.runs_dir()`` creates the
    directory, and merely importing this module — which every request path does,
    tracing on or off — must not litter the filesystem.
    """
    override = os.getenv("TRACE_DIR")
    if override:
        d = Path(override).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        return d
    return paths.runs_dir()

_current: ContextVar["Tracer | None"] = ContextVar("current_trace", default=None)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _enabled() -> bool:
    return os.getenv("TRACE", "1").strip().lower() in {"1", "true", "yes", "on"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str | None, *, maxlen: int = 40) -> str:
    if not text:
        return ""
    s = _SLUG_RE.sub("-", str(text).lower()).strip("-")
    return s[:maxlen].strip("-")


# ── the real tracer ──────────────────────────────────────────────────────────
class Tracer:
    """Writes artifacts into one run directory and keeps a live ``meta.json``."""

    def __init__(self, path: Path, meta: dict) -> None:
        self.path = path
        self.meta = meta
        self._flush()

    # -- artifact writers -----------------------------------------------------
    def write(self, name: str, text: str, *, unique: bool = False) -> None:
        """Write ``text`` to ``name`` in the run dir. With ``unique=True`` a
        repeated ``name`` is auto-suffixed (``retrieval.md`` -> ``retrieval_2.md``)
        so multi-call stages (e.g. deepsummary's many answers) don't clobber."""
        target = self.path / name
        if unique and target.exists():
            stem, _, ext = name.rpartition(".")
            stem, ext = (stem, ext) if stem else (name, "")
            i = 2
            while target.exists():
                target = self.path / (f"{stem}_{i}.{ext}" if ext else f"{stem}_{i}")
                i += 1
        target.write_text(text, encoding="utf-8")

    def append(self, name: str, text: str) -> None:
        with (self.path / name).open("a", encoding="utf-8") as fh:
            fh.write(text)

    def json(self, name: str, obj) -> None:
        import json as _json

        (self.path / name).write_text(
            _json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )

    def note(self, text: str) -> None:
        self.append("log.txt", f"{_now()}  {text}\n")

    # -- metadata / timing ----------------------------------------------------
    def set(self, **fields) -> None:
        self.meta.update(fields)
        self._flush()

    @contextmanager
    def timer(self, stage: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.meta.setdefault("timings", {})[stage] = round(time.perf_counter() - t0, 3)
            self._flush()

    def _flush(self) -> None:
        import json as _json

        (self.path / "meta.json").write_text(
            _json.dumps(self.meta, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )


# ── the no-op tracer (active when tracing is off / no run open) ───────────────
class _NullTracer:
    path = None

    def write(self, *a, **k) -> None: ...
    def append(self, *a, **k) -> None: ...
    def json(self, *a, **k) -> None: ...
    def note(self, *a, **k) -> None: ...
    def set(self, *a, **k) -> None: ...

    def timer(self, stage: str):
        return contextlib.nullcontext()


_NULL = _NullTracer()


# ── public surface ───────────────────────────────────────────────────────────
def current_run() -> "Tracer | _NullTracer":
    """The active tracer, or a no-op tracer if tracing is off / no run is open."""
    return _current.get() or _NULL


def _next_run_dir(kind: str, label: str | None) -> Path:
    runs = _runs_dir()
    nums = [
        int(m.group(1))
        for d in runs.iterdir()
        if d.is_dir() and (m := re.match(r"run(\d+)_", d.name))
    ]
    n = (max(nums) + 1) if nums else 1
    suffix = _slug(label)
    while True:
        name = f"run{n}_{kind}" + (f"_{suffix}" if suffix else "")
        try:
            path = runs / name
            path.mkdir()  # exclusive: raises if another process took this number
            return path
        except FileExistsError:
            n += 1


@contextmanager
def begin_run(kind: str, label: str | None = None, **meta):
    """Open a run for the duration of the ``with`` block and make it the active
    tracer. Yields a :class:`Tracer` (or a no-op tracer when disabled)."""
    if not _enabled():
        yield _NULL
        return
    tr = Tracer(_next_run_dir(kind, label), {"kind": kind, "label": label, "started_at": _now(), **meta})
    logger.info("trace: %s", tr.path)
    token = _current.set(tr)
    try:
        yield tr
    except Exception as exc:  # noqa: BLE001 - record then re-raise
        tr.set(error=repr(exc))
        raise
    finally:
        tr.set(ended_at=_now())
        _current.reset(token)


def project_artifact(project_id, name: str, text: str) -> None:
    """Write a project-scoped artifact (``runs/project_<id8>/<name>``), used by
    the project-wide worker jobs (clusters/projections) which run outside any
    single run. No-op when tracing is disabled."""
    if not _enabled():
        return
    pdir = _runs_dir() / f"project_{str(project_id)[:8]}"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / name).write_text(text, encoding="utf-8")


# ── shared renderers (kept generic so tap sites stay one-liners) ─────────────
def render_prefs(prefs, **extra) -> str:
    """Render effective Preferences (+ any extras like intent/scope/strategy) as
    a transparent markdown dump, system prompt shown verbatim at the end."""
    d = asdict(prefs) if is_dataclass(prefs) else dict(prefs)
    system_prompt = d.pop("system_prompt", "")
    lines = ["# Effective preferences\n"]
    for k, v in extra.items():
        lines.append(f"- **{k}**: `{v}`")
    if extra:
        lines.append("")
    for k, v in d.items():
        lines.append(f"- **{k}**: `{v}`")
    lines.append("\n## system_prompt\n")
    lines.append("```\n" + str(system_prompt) + "\n```")
    return "\n".join(lines)


def render_messages(messages: list[dict]) -> str:
    """Render the exact messages array sent to the LLM (full transparency)."""
    out = ["# Prompt sent to the LLM\n"]
    for m in messages:
        out.append(f"## role: {m.get('role')}\n")
        out.append("```\n" + str(m.get("content", "")) + "\n```\n")
    return "\n".join(out)
