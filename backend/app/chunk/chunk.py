r"""
Structure-aware Markdown chunking for the ingestion pipeline.

Turns the Markdown that ``parse()`` produces into a list of :class:`Chunk`s, each
carrying everything the worker needs to write a row: the original text
(``content_full``), an algorithmically compressed version (``content_sparse``), a
Postgres ``ltree`` path derived from the heading hierarchy, and token bookkeeping.

This module is deliberately pure: Markdown in, ``list[Chunk]`` out. No DB, no
network, no embeddings — that keeps it fast, deterministic and unit-testable. The
worker is responsible for persistence.

Three jobs, mirroring PHASE4_PLAN_v2 §2.B:

* **Structural split.** Split on Markdown headings, then greedily pack the pieces
  to a 400-800 token target. Math (``$$...$$`` / ``$...$``) is an indivisible
  unit — an equation is never split across two chunks, even if that pushes a
  chunk over the token cap.
* **The skeleton (``ltree``).** Each heading's slug becomes a path label
  (``doc.ch2.backprop``), giving a database-native table of contents. Sibling
  headings that slugify to the same label are disambiguated (``_2``, ``_3``).
* **Algorithmic compression (``content_sparse``).** A regex + stopword pass strips
  pedagogical filler while keeping headings, LaTeX, bold, bullets, numbers and
  capitalised terms — "caveman math speak" an LLM can reconstruct from. spaCy /
  TextRank is a deliberate future upgrade; we start dependency-light.
"""
from __future__ import annotations

import logging
import re
import statistics
import string
import unicodedata
from dataclasses import dataclass, field

# Allow running this file directly (`python app/chunk/chunk.py`) as well as
# `python -m app.chunk.chunk`: with no package context, put the backend root on
# sys.path so the `app.*` import below resolves. Same guard as app/embed/embed.py.
if __package__ in (None, ""):
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

# Sizing, not a hard limit: chunk packing tolerates a few percent of drift, and
# errors average out across a whole chunk. See app/tokens.py.
from app.tokens import estimate as count_tokens

logger = logging.getLogger(__name__)

# Token target per chunk (PHASE4_PLAN_v2 §2.B). Soft bounds: an indivisible atom
# bigger than MAX (a long equation, say) is allowed to overflow rather than split.

# resized to make it more granular
MIN_TOKENS = 400
MAX_TOKENS = 800

# Max characters for one ltree label — keeps paths readable and well under
# Postgres' 256-byte label limit.
_LABEL_MAX = 40


@dataclass(frozen=True)
class Chunk:
    chunk_index: int
    content_full: str          # original Markdown slice
    content_sparse: str        # compressed "caveman math speak"
    path: str                  # ltree, e.g. "doc.attention.self_attention"
    heading_trail: list[str] = field(default_factory=list)  # human headings, for UI/metadata
    token_count: int = 0       # tokens in content_full


# ---------------------------------------------------------------------------
# Slugifier (the §6 open question): deterministic, ASCII, sibling-deduplicated.
# ---------------------------------------------------------------------------
def _slugify(text: str) -> str:
    """Heading -> a safe ltree label: ascii, lowercase, ``[a-z0-9_]`` only."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if len(text) > _LABEL_MAX:
        text = text[:_LABEL_MAX].rstrip("_")
    return text


def _dedupe(used: set[str], label: str) -> str:
    """Make ``label`` unique among its siblings by appending _2, _3, ..."""
    if label not in used:
        used.add(label)
        return label
    n = 2
    while f"{label}_{n}" in used:
        n += 1
    out = f"{label}_{n}"
    used.add(out)
    return out


# ---------------------------------------------------------------------------
# Pass 1: split Markdown into heading-scoped sections.
# ---------------------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


@dataclass
class _Section:
    title_trail: list[str]  # human headings from H1 down to this one
    slug_trail: list[str]   # ltree labels, doc_slug-prefixed
    body: list[str]         # the section's lines (heading line included)


def _split_sections(markdown: str, doc_slug: str) -> list[_Section]:
    """Walk the Markdown, tracking a heading stack, into heading-scoped sections.

    Slug labels are resolved here (with sibling dedup) so everything downstream
    just takes common prefixes of already-unique label lists. Fenced code blocks
    are tracked so a ``#`` inside one is never mistaken for a heading.
    """
    stack: list[tuple[int, str, str]] = []      # (level, title, label)
    used_children: dict[tuple[str, ...], set[str]] = {}
    sections = [_Section(title_trail=[], slug_trail=[doc_slug], body=[])]
    in_fence = False

    for line in markdown.split("\n"):
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            sections[-1].body.append(line)
            continue

        m = None if in_fence else _HEADING_RE.match(line)
        if not m:
            sections[-1].body.append(line)
            continue

        level, title = len(m.group(1)), m.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_key = tuple(lbl for _, _, lbl in stack) or (doc_slug,)
        base = _slugify(title) or f"s{len(sections)}"
        label = _dedupe(used_children.setdefault(parent_key, set()), base)
        stack.append((level, title, label))
        sections.append(_Section(
            title_trail=[t for _, t, _ in stack],
            slug_trail=[doc_slug] + [lbl for _, _, lbl in stack],
            body=[line],
        ))

    # Drop the empty preamble placeholder if the doc opened straight on a heading.
    return [sec for sec in sections if any(ln.strip() for ln in sec.body)]


# ---------------------------------------------------------------------------
# Pass 2: atomise + pack. An "atom" is the smallest indivisible unit — a
# paragraph, a bullet, or a whole math/code block. Equations stay whole.
# ---------------------------------------------------------------------------
@dataclass
class _Atom:
    text: str
    slug_trail: list[str]
    title_trail: list[str]
    tokens: int


def _atomize(body: list[str]) -> list[str]:
    """Split a section body into paragraph atoms on blank lines, but keep
    ``$$...$$`` math blocks and ```` ``` ```` code fences whole so an equation is
    never broken apart."""
    atoms: list[str] = []
    buf: list[str] = []
    in_fence = in_math = False

    def flush() -> None:
        nonlocal buf
        if any(ln.strip() for ln in buf):
            atoms.append("\n".join(buf).strip("\n"))
        buf = []

    for line in body:
        s = line.strip()
        if s.startswith("```"):
            if not in_fence:
                flush()
            buf.append(line)
            in_fence = not in_fence
            if not in_fence:
                flush()
            continue
        if s == "$$" and not in_fence:
            if not in_math:
                flush()
            buf.append(line)
            in_math = not in_math
            if not in_math:
                flush()
            continue
        if not in_fence and not in_math and s == "":
            flush()
        else:
            buf.append(line)
    flush()
    return atoms


# Ideal chunk size: the geometric mean of the band, used to score pivot levels.
_TARGET_TOKENS = (MIN_TOKENS * MAX_TOKENS) ** 0.5

# A group this small is just an ancestor heading (e.g. a bare "# Chapter 2" with
# no intro prose before its first subsection). We let it ride forward across a
# pivot boundary rather than emit it as a standalone heading-only chunk.
_STUB_TOKENS = 16


def _pivot_key(atom: _Atom, pivot: int | None) -> tuple[str, ...] | None:
    """The atom's heading path truncated to the pivot nesting depth — the unit we
    refuse to merge across. ``None`` in flat mode (no structural boundary)."""
    return None if pivot is None else tuple(atom.slug_trail[: pivot + 1])


def _pick_pivot(atoms: list[_Atom]) -> int | None:
    """Choose the heading *nesting depth* to treat as the chunk boundary.

    "Depth" is stack nesting depth, not the Markdown heading number, so a deck of
    bare ``##`` slides (no H1) sits at depth 1 just like a textbook's top chapters
    — the choice is level-agnostic.

    For each depth present we group atoms into the contiguous sections that depth
    would produce and score the grouping by how close its median section size is
    to the target band. The closest wins. We only fall back to flat (``None``)
    when even the best grouping is *far* too granular — its median below half the
    floor — meaning every structural level over-segments (e.g. a deck of ~50-token
    slides). A merely-below-MIN median (a textbook's ~360-token sections) is still
    a good boundary and is kept. No headings at all also returns ``None``.
    """
    max_depth = max((len(a.slug_trail) - 1 for a in atoms), default=0)
    if max_depth == 0:
        return None

    best_depth, best_dev, best_median = None, None, None
    for depth in range(1, max_depth + 1):
        sizes, key, total = [], object(), 0
        for a in atoms:
            k = tuple(a.slug_trail[: depth + 1])
            if k != key:
                if total:
                    sizes.append(total)
                key, total = k, 0
            total += a.tokens
        if total:
            sizes.append(total)
        median = statistics.median(sizes)
        dev = abs(median - _TARGET_TOKENS)
        if best_dev is None or dev < best_dev:
            best_depth, best_dev, best_median = depth, dev, median

    return None if best_median < MIN_TOKENS / 2 else best_depth


def _pack(atoms: list[_Atom], pivot: int | None) -> list[list[_Atom]]:
    """Greedily pack atoms into ~400-800 token groups, honouring ``pivot``.

    A group is closed before adding the next atom when either:

    * the atom crosses a pivot boundary — its pivot-depth heading path differs
      from the *previous* atom's — so we never merge two major sections together,
      even if the current group is still small; or
    * the group has reached MIN_TOKENS and adding the atom would exceed
      MAX_TOKENS — the normal cap, which splits a long section into MAX pieces.

    The boundary is the transition *between consecutive atoms*, not against the
    group's first atom: that way a stub ancestor heading (a bare ``# Chapter 2``
    whose pivot key is the parent, not a section) is simply absorbed into the
    following section rather than triggering a split on every paragraph.

    In flat mode (``pivot is None``) there are no boundaries, so a run of tiny
    sibling slides coalesces up toward the target instead of emitting dozens of
    fragments. A single atom larger than MAX_TOKENS (e.g. a long equation) becomes
    its own oversized group — we never split it.
    """
    groups: list[list[_Atom]] = []
    cur: list[_Atom] = []
    cur_tokens = 0
    prev_key: tuple[str, ...] | None = None
    for a in atoms:
        a_key = _pivot_key(a, pivot)
        if cur:
            crossed = a_key != prev_key and cur_tokens >= _STUB_TOKENS
            too_big = cur_tokens >= MIN_TOKENS and cur_tokens + a.tokens > MAX_TOKENS
            if crossed or too_big:
                groups.append(cur)
                cur, cur_tokens = [], 0
        cur.append(a)
        cur_tokens += a.tokens
        prev_key = a_key
    if cur:
        groups.append(cur)
    return groups


# ---------------------------------------------------------------------------
# Sparse compression: regex + stopwords, math/headings/tables protected.
# ---------------------------------------------------------------------------
_MATH_BLOCK = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_MATH_INLINE = re.compile(r"\$[^$\n]+?\$")
_BULLET_RE = re.compile(r"^(\s*(?:[-*+]|\d+\.)\s+)(.*)$")

# Compact, hand-picked English stopword set. Small on purpose: we only want to
# shed obvious filler, not aggressively rewrite meaning.
_STOPWORDS = frozenset("""
a an the this that these those of in on at to for from by with without into onto
over under and or but nor so yet as if then than is are was were be been being am
do does did doing have has had having will would shall should can could may might
must it its it's we us our you your they them their he she his her i me my mine
about above below up down out off again further once here there all any both each
few more most other some such only own same too very just also which who whom
whose what when where why how because while during before after between against
""".split())


def _keep_token(tok: str) -> bool:
    """Whether a prose token survives compression. Keeps math placeholders, bold,
    numbers, capitalised words (proper nouns / key terms) and pure symbols; drops
    only lowercase stopwords."""
    if "\x00" in tok:                       # protected math placeholder
        return True
    if tok.startswith("**") or tok.endswith("**"):  # bold
        return True
    core = tok.strip(string.punctuation)
    if not core:                            # pure punctuation / math operator
        return True
    if any(ch.isdigit() for ch in core):    # numbers / equation tokens
        return True
    if core[0].isupper():                   # proper noun / keyword
        return True
    return core.lower() not in _STOPWORDS


def _compress_line(line: str) -> str:
    s = line.strip()
    if not s or s.startswith("#") or s.startswith("|") or s.startswith("$$"):
        return line  # headings, tables and block-math lines kept verbatim
    m = _BULLET_RE.match(line)
    prefix, rest = (m.group(1), m.group(2)) if m else ("", line)
    kept = [t for t in rest.split() if _keep_token(t)]
    if not kept:
        return ""
    return prefix + " ".join(kept)


def _compress(text: str) -> str:
    """Strip pedagogical fluff while protecting math, headings, tables and bold.

    Math spans are stashed behind ``\\x00n\\x00`` placeholders before the prose
    pass so a stopword strip can never touch LaTeX, then restored verbatim.
    """
    spans: list[str] = []

    def stash(m: re.Match) -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"

    masked = _MATH_INLINE.sub(stash, _MATH_BLOCK.sub(stash, text))
    body = "\n".join(_compress_line(ln) for ln in masked.split("\n"))
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], body)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------
def chunk(markdown: str, *, doc_slug: str) -> list[Chunk]:
    """Split ``markdown`` into structure-aware, token-bounded chunks.

    Args:
        markdown: the document Markdown (from :func:`app.ingest.parse.parse`).
        doc_slug: the root ltree label for this document (already slugified by
                  the caller, e.g. from the document id/name).

    Returns:
        Chunks in document order, each with ``content_full``, ``content_sparse``,
        an ltree ``path``, the human ``heading_trail`` and a ``token_count``.
    """
    doc_slug = _slugify(doc_slug) or "doc"

    atoms: list[_Atom] = []
    for sec in _split_sections(markdown, doc_slug):
        for text in _atomize(sec.body):
            atoms.append(_Atom(text, sec.slug_trail, sec.title_trail, count_tokens(text)))
    if not atoms:
        return []

    pivot = _pick_pivot(atoms)
    logger.info("chunk: pivot depth = %s (%d atoms)", pivot, len(atoms))

    chunks: list[Chunk] = []
    for i, group in enumerate(_pack(atoms, pivot)):
        content_full = "\n\n".join(a.text for a in group)
        # Path/trail come from the section the chunk *starts* in: a specific leaf
        # rather than the common ancestor (which collapses to the bare root when a
        # chunk coalesces several flat sibling slides, erasing ltree granularity).
        path = ".".join(group[0].slug_trail)
        trail = list(group[0].title_trail)
        chunks.append(Chunk(
            chunk_index=i,
            content_full=content_full,
            content_sparse=_compress(content_full),
            path=path,
            heading_trail=trail,
            token_count=count_tokens(content_full),
        ))
    return chunks


# ---------------------------------------------------------------------------
# Manual test harness:  python -m app.chunk.chunk
# Reads the Markdown that the parser harness wrote (app/ingest/test_output.md)
# and prints chunk stats so you can eyeball paths, sizing and compression.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import dataclasses
    import json
    import statistics
    import sys
    from pathlib import Path

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    md_path = Path(__file__).parent.parent / "ingest" / "test_output.md"
    if not md_path.exists():
        raise SystemExit(f"Run the parser harness first to produce {md_path}")

    chunks = chunk(md_path.read_text(encoding="utf-8"), doc_slug="test")
    full_tok = sum(c.token_count for c in chunks)
    sparse_tok = sum(count_tokens(c.content_sparse) for c in chunks)
    sizes = [c.token_count for c in chunks]

    print(f"{len(chunks)} chunks from {md_path.name}")
    print(f"tokens/chunk: min={min(sizes)} median={int(statistics.median(sizes))} max={max(sizes)}")
    print(f"sparse compression: {full_tok} -> {sparse_tok} tokens "
          f"({100 * (1 - sparse_tok / full_tok):.0f}% reduction)")
    print("\nsample paths:")
    for c in chunks[:8]:
        print(f"  [{c.token_count:>4}t] {c.path}")
    print("\n----- first chunk content_sparse -----\n")
    print(chunks[0].content_sparse[:800])

    out_path = Path(__file__).parent / "test_chunks.json"
    out_path.write_text(
        json.dumps([dataclasses.asdict(c) for c in chunks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nAll {len(chunks)} chunks written to: {out_path}")
