"""
PDF parsing via Docling — turns raw PDF bytes into structural Markdown.

Everything Docling-specific is hidden behind `parse(pdf_bytes) -> str`, so the
rest of the pipeline (chunking, embedding) never imports Docling directly. Docling
runs locally on CPU, costs nothing per page, and is good at preserving the things
STEM material lives or dies by: headings, LaTeX, and tables.

Heavy by nature: the first call loads Docling's ML models (layout + table
structure). We build the DocumentConverter lazily and cache it on the module, so a
long-lived worker process pays that cost once and reuses the models afterwards.

Three deliberate choices, the first two learned the hard way on a real STEM PDF:

* **Backend = pypdfium2.** Docling's default `docling-parse` backend mangled text
  on a notes PDF with custom font encoding ("Scattered" came out as "Sca tt e r e
  d"). pypdfium2 extracts the same text cleanly, so we pin it as the backend.
* **Fail loud on partial conversions.** Docling can drop individual pages (e.g.
  `std::bad_alloc` while rendering) and still report overall "success-ish". For a
  RAG pipeline, silently ingesting 88% of a document is a data-integrity bug, so
  `parse()` raises on anything short of a clean SUCCESS unless `allow_partial`.
* **Formula enrichment on.** We enable `do_formula_enrichment` so equations are
  emitted as LaTeX (`$...$`) rather than flattened to plain text — STEM math
  fidelity is the point. Costs an extra model + parse time.
"""
from __future__ import annotations

import logging
import os
import re
from io import BytesIO

# Must be set before huggingface_hub is imported. On Windows, model caching tries
# to create symlinks, which needs admin/Developer Mode and otherwise raises
# WinError 1314; this tells HF to copy files instead. Harmless on Linux/Mac.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

logger = logging.getLogger(__name__)

# Cached, lazily-initialised converter. Building it loads ML models, so we never
# do it at import time — only on the first actual parse() call.
_converter = None


class ParseError(RuntimeError):
    """Raised when Docling fails to fully convert a document."""


# Fraction of an earlier page's tokens that must reappear on the next page for us
# to treat it as a beamer overlay build-up (and drop it). Strict containment with
# slack for extraction jitter / reflow, per design discussion.
_OVERLAY_CONTAINMENT_THRESHOLD = 0.90


def _page_tokens(text: str) -> list[str]:
    """Whitespace-split, lower-cased tokens. Good enough to compare page content;
    math is extracted glyph-by-glyph with spaces, so it tokenises fine here."""
    return text.lower().split()


def _containment(earlier: list[str], later: list[str]) -> float:
    """Directional overlap: what fraction of `earlier`'s tokens (as a multiset)
    also appear in `later`. 1.0 means `earlier` is fully contained in `later`.

    An empty earlier page counts as fully contained (1.0) so title-only
    intermediate overlays get folded into the fuller page that follows.
    """
    if not earlier:
        return 1.0
    from collections import Counter

    later_counts = Counter(later)
    covered = 0
    for tok, need in Counter(earlier).items():
        covered += min(need, later_counts.get(tok, 0))
    return covered / len(earlier)


def _overlay_keeper_indices(page_texts: list[str], threshold: float = _OVERLAY_CONTAINMENT_THRESHOLD) -> list[int]:
    """Given each page's extracted text, return the indices to KEEP.

    A page is dropped when it is ~contained in the page immediately after it —
    the signature of a beamer incremental reveal, where each overlay accumulates
    onto the previous one. Because overlays grow monotonically, a pairwise
    "is page N inside page N+1?" test naturally keeps the last (fullest) page of
    each run without needing explicit run grouping.
    """
    tokens = [_page_tokens(t) for t in page_texts]
    keep = []
    for i in range(len(tokens)):
        is_last = i == len(tokens) - 1
        if not is_last and _containment(tokens[i], tokens[i + 1]) >= threshold:
            continue  # page i is an earlier overlay of page i+1 — drop it
        keep.append(i)
    return keep


def _strip_overlay_pages(pdf_bytes: bytes) -> bytes:
    """Drop beamer overlay-duplicate pages *before* Docling sees them.

    Beamer emits one physical PDF page per incremental reveal (``\\pause`` etc.),
    so a deck can carry 2-4x the pages it has logical slides. Docling runs its
    layout / table / formula models on *every* page, so those duplicates are pure
    wasted compute — the slow part of parsing. We use pypdfium2 (text extraction
    only, no ML, near-instant) to spot consecutive overlay build-ups and rebuild
    a trimmed PDF containing just the final, fully-revealed page of each run.

    Best-effort: any failure falls back to the original bytes so a quirk in the
    fast path can never block ingestion. Returns the original bytes unchanged
    when nothing is dropped (no needless re-encode).
    """
    try:
        import pypdfium2 as pdfium

        src = pdfium.PdfDocument(pdf_bytes)
        try:
            page_texts = []
            for page in src:
                textpage = page.get_textpage()
                page_texts.append(textpage.get_text_range())
                textpage.close()
                page.close()

            keep = _overlay_keeper_indices(page_texts)
            if len(keep) == len(page_texts):
                return pdf_bytes  # nothing to drop

            trimmed = pdfium.PdfDocument.new()
            try:
                trimmed.import_pages(src, keep)
                buffer = BytesIO()
                trimmed.save(buffer)
            finally:
                trimmed.close()

            logger.info(
                "parse: dropped %d overlay page(s) before model pass (%d -> %d pages)",
                len(page_texts) - len(keep), len(page_texts), len(keep),
            )
            return buffer.getvalue()
        finally:
            src.close()
    except Exception as exc:  # noqa: BLE001 - fast path must never block ingestion
        logger.warning("parse: overlay page-strip skipped (%s); parsing full PDF", exc)
        return pdf_bytes


def _dedupe_overlay_slides(markdown: str) -> str:
    r"""
    Collapse beamer-style overlay duplicates in slide-deck Markdown.

    Lecture decks built with LaTeX beamer emit one PDF *page per incremental
    reveal* (``\pause``, ``\onslide``, ``\uncover``). A single logical slide
    therefore shows up as 2-4 near-identical PDF pages that build up step by
    step, and Docling — faithfully — extracts every one of them. The result is
    the same slide repeated back-to-back in the Markdown.

    Worse, Docling's formula enrichment is *not* consistent across these
    near-identical pages. On a partial-overlay page the equation region often
    isn't classified as a formula, so its glyphs get dumped as scattered plain
    text ("p ψ RL = p θ (y|x) ..."); on the final, fully-revealed page the same
    region is recognised and emitted as clean LaTeX (``$$...$$``). So the
    duplicate isn't merely noise — one copy is mangled and one is clean.

    Heuristic: Docling renders each slide title as an H2 (``## ...``), so we
    treat each ``##`` heading as the start of a slide section. For any run of
    *consecutive* sections sharing the exact same heading, we keep only the
    last. In beamer build order the last page is the fully-revealed slide, which
    is both the most complete (overlays accumulate, so it is a superset of the
    earlier ones) and the copy whose formulas Docling actually recognised.

    For non-beamer PDFs (no consecutive duplicate headings) this is a no-op.
    """
    sections: list[list] = []  # each entry: [heading_key | None, [lines...]]
    for line in markdown.split("\n"):
        if line.startswith("## "):
            sections.append([line.strip(), [line]])
        elif sections:
            sections[-1][1].append(line)
        else:
            # Preamble before the first heading (title slide, stray lines).
            sections.append([None, [line]])

    deduped: list[list] = []
    dropped = 0
    for key, body in sections:
        if key is not None and deduped and deduped[-1][0] == key:
            deduped[-1] = [key, body]  # replace the earlier overlay with this one
            dropped += 1
        else:
            deduped.append([key, body])

    if dropped:
        logger.info("parse: collapsed %d duplicate overlay slide(s)", dropped)

    return "\n".join("\n".join(body) for _, body in deduped)


# A standalone "12/49"-style slide-number line that beamer stamps on each page.
_SLIDE_NUMBER_RE = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")
# Glyphs beamer uses for bullets; converted to Markdown "- " in the fast path.
_BULLET_GLYPHS = {"▶", "•", "‣", "◦", "▪", "·"}


def _format_page_markdown(text: str) -> str:
    """Light Markdown shaping of one page's raw text — purely cosmetic, drops no
    content: strip a leading slide-number line, promote the first line to an H2
    title (so the structure-aware chunker still sees slide boundaries), and turn
    beamer bullet glyphs (▶ etc.) into Markdown list items."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if lines and _SLIDE_NUMBER_RE.match(lines[0]):
        lines = lines[1:]
    if not lines:
        return ""
    out = [f"## {lines[0].strip()}"]
    for ln in lines[1:]:
        stripped = ln.lstrip()
        if stripped[:1] in _BULLET_GLYPHS:
            out.append(f"- {stripped[1:].lstrip()}")
        else:
            out.append(ln)
    return "\n".join(out)


def _extract_text_fast(pdf_bytes: bytes, *, name: str = "document.pdf") -> str:
    r"""
    Fast, model-free extraction (the default backend for parse()).

    Pulls the PDF's native text layer with pypdfium2 and lightly formats it as
    Markdown. No layout / table / formula models run, so it is near-instant and
    free — the full 75-page test deck extracts in ~0.1s versus ~3.5 min for the
    model pipeline.

    Deliberate tradeoff: equations come out as the raw glyph sequence
    ("ψ ∗ = argmax ψ X y∈S Uϕ(y,x)−β log(...)") rather than rendered LaTeX, and
    tables flatten to text. For born-digital lecture slides the prose, bullets,
    and the *tokens* of each equation all survive, which is enough for an LLM to
    reason between the lines. Switch to parse(..., use_models=True) when faithful
    LaTeX / table structure is required.

    Overlay dedup still applies: we reuse _overlay_keeper_indices to drop beamer
    ``\pause`` build-up pages, keeping only the final, fullest page of each run.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        page_texts = []
        for page in doc:
            textpage = page.get_textpage()
            page_texts.append(textpage.get_text_range())
            textpage.close()
            page.close()
    finally:
        doc.close()

    keep = _overlay_keeper_indices(page_texts)
    if len(keep) != len(page_texts):
        logger.info(
            "parse: fast text path dropped %d overlay page(s) (%d -> %d)",
            len(page_texts) - len(keep), len(page_texts), len(keep),
        )

    blocks = (_format_page_markdown(page_texts[i]) for i in keep)
    markdown = "\n\n".join(b for b in blocks if b)
    # Safety net for any residual consecutive duplicate slide titles.
    return _dedupe_overlay_slides(markdown)


def _get_converter():
    global _converter
    if _converter is None:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        logger.info("parse: initialising Docling DocumentConverter (loading models)")

        # Formula enrichment: detect equation regions and convert them to LaTeX so
        # math survives as `$...$` instead of being flattened to plain text. Costs
        # an extra model (CodeFormula) + some parse time, but STEM fidelity is the
        # whole point of this tool. Downloaded on first run.
        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_formula_enrichment = True

        _converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pipeline_options,
                    backend=PyPdfiumDocumentBackend,
                )
            }
        )
    return _converter


def parse(
    pdf_bytes: bytes,
    *,
    name: str = "document.pdf",
    allow_partial: bool = False,
    use_models: bool = False,
) -> str:
    """
    Convert a PDF (raw bytes) into Markdown. Two backends:

    * **Fast (default, ``use_models=False``)** — :func:`_extract_text_fast`:
      pypdfium2 native-text extraction + overlay dedup. Near-instant, free, no
      ML. Equations stay as raw glyph text and tables flatten, but prose, bullets
      and equation tokens survive — enough for an LLM to reason between the lines.
      ``allow_partial`` is ignored (pypdfium2 either reads the PDF or raises).
    * **Model (``use_models=True``)** — :func:`_parse_with_models`: the full
      Docling pipeline (layout + table + formula→LaTeX). Faithful LaTeX and table
      structure, but slow on CPU (minutes for a lecture deck); worth it on GPU or
      when math/table fidelity is required.

    Args:
        pdf_bytes:     the raw bytes of the PDF file.
        name:          a filename hint used for format detection / logging.
        allow_partial: (model backend only) if False, raise ParseError when
                       Docling fails to convert the whole document; if True,
                       return whatever Markdown was produced and log the rest.
        use_models:    opt into the slow, high-fidelity Docling backend.

    Returns:
        Markdown text. The fast backend gives headings + bullets + plain-text
        math; the model backend additionally gives LaTeX ($...$ / $$...$$) and
        structured tables.

    Raises:
        ParseError: (model backend) on a failed or non-allow_partial conversion.
    """
    if not use_models:
        return _extract_text_fast(pdf_bytes, name=name)
    return _parse_with_models(pdf_bytes, name=name, allow_partial=allow_partial)


def _parse_with_models(pdf_bytes: bytes, *, name: str = "document.pdf", allow_partial: bool = False) -> str:
    """High-fidelity Docling backend. See :func:`parse` for the tradeoffs."""
    from docling.datamodel.base_models import ConversionStatus, DocumentStream

    # Drop beamer overlay-duplicate pages up front (fast, pypdfium2 text only) so
    # Docling's slow per-page models don't run on 2-4x redundant pages.
    pdf_bytes = _strip_overlay_pages(pdf_bytes)

    converter = _get_converter()
    source = DocumentStream(name=name, stream=BytesIO(pdf_bytes))
    result = converter.convert(source, raises_on_error=False)

    if result.status == ConversionStatus.FAILURE:
        details = "; ".join(str(e) for e in result.errors) or "no error detail"
        raise ParseError(f"Docling failed to convert {name}: {details}")

    if result.status != ConversionStatus.SUCCESS:
        # PARTIAL_SUCCESS / SKIPPED / PENDING — some pages didn't make it.
        details = "; ".join(str(e) for e in result.errors) or "no error detail"
        msg = f"Docling only partially converted {name} (status={result.status.value}): {details}"
        if not allow_partial:
            raise ParseError(msg)
        logger.warning("parse: %s", msg)

    markdown = result.document.export_to_markdown()
    # Safety net: _strip_overlay_pages already removed most overlays before the
    # model pass, but catch any residual consecutive duplicate slides at the
    # Markdown level (keeps the final, clean-LaTeX copy of each).
    return _dedupe_overlay_slides(markdown)


# ---------------------------------------------------------------------------
# Manual test harness:  python -m app.ingest.parse            (fast text backend)
#                       python -m app.ingest.parse --models   (Docling backend)
#
# Parses `test.pdf` sitting next to this module (app/ingest/test.pdf), prints a
# few stats, and writes the Markdown to `test_output.md` (fast) or
# `test_output_models.md` (--models) so you can eyeball the fidelity.
#
# The __main__ guard + freeze_support() matter on Windows: Docling/torch spawn
# worker processes, and spawned children re-import the main module. Without the
# guard the test body would run again in every child.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import multiprocessing
    import sys
    import time
    from pathlib import Path

    multiprocessing.freeze_support()
    # The Windows console is cp1252 and chokes on LaTeX arrows etc. when we print
    # the markdown preview; force stdout to UTF-8 so the harness never crashes on it.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    use_models = "--models" in sys.argv
    pdf_path = Path(__file__).parent / "test.pdf"
    if not pdf_path.exists():
        raise SystemExit(f"Put a PDF at {pdf_path.resolve()} to test the parser.")

    pdf_bytes = pdf_path.read_bytes()
    backend = "Docling model" if use_models else "fast text"
    print(f"Parsing {pdf_path.name} ({len(pdf_bytes):,} bytes) via {backend} backend...")

    start = time.perf_counter()
    # allow_partial here so we can SEE the output even when pages drop, and report it.
    markdown = parse(pdf_bytes, name=pdf_path.name, allow_partial=True, use_models=use_models)
    elapsed = time.perf_counter() - start

    out_name = "test_output_models.md" if use_models else "test_output.md"
    out_path = Path(__file__).parent / out_name
    out_path.write_text(markdown, encoding="utf-8")

    print(f"\nDone in {elapsed:.1f}s")
    print(f"Markdown length: {len(markdown):,} chars")
    print(f"Full output written to: {out_path.resolve()}")
    # print("\n----- first 1500 chars -----\n")
    # print(markdown[:1500])
