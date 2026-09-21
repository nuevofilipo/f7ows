"""
Token estimation: a tokenizer-free replacement for tiktoken.
"""
from __future__ import annotations

# Measured document-level mean over the parsed corpus. See the harness below.
CHARS_PER_TOKEN = 3.67


def estimate(text: str) -> int:
    """Approximate token count for ``text``."""
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def clip(text: str, max_tokens: int) -> str:
    """Trim ``text`` down to roughly ``max_tokens``, cutting on a word boundary
    when one is near the end so it doesn't stop mid-word."""
    if estimate(text) <= max_tokens:
        return text
    limit = int(max_tokens * CHARS_PER_TOKEN)
    clipped = text[:limit]
    boundary = clipped.rfind(" ")
    if boundary > limit * 0.9:  # only if we're not throwing away real content
        clipped = clipped[:boundary]
    return clipped


# ---------------------------------------------------------------------------
# Calibration harness:  python -m app.tokens [glob ...]
#
# Re-measures CHARS_PER_TOKEN against real tiktoken counts. Run it if the corpus
# changes character (much more/less LaTeX) or the embedding model's tokenizer
# does. Requires tiktoken to be installed *in the dev venv only* — it is
# deliberately not a runtime dependency.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import glob
    import statistics
    import sys

    patterns = sys.argv[1:] or ["runs/**/00_parsed.md"]
    try:
        import tiktoken
    except ImportError:
        raise SystemExit("pip install tiktoken (dev only) to run the calibration harness")

    enc = tiktoken.get_encoding("o200k_base")
    docs, paras, seen = [], [], set()
    for pattern in patterns:
        for path in sorted(glob.glob(pattern, recursive=True)):
            text = open(path, encoding="utf-8").read()
            if len(text) < 500 or text in seen:
                continue
            seen.add(text)
            docs.append(len(text) / len(enc.encode(text)))
            for para in text.split("\n\n"):
                para = para.strip()
                if len(para) >= 80:
                    paras.append((len(para), len(enc.encode(para))))

    if not docs:
        raise SystemExit(f"no documents matched {patterns}")

    mean = statistics.mean(docs)
    errors = sorted(((c / CHARS_PER_TOKEN) - n) / n for c, n in paras)
    print(f"{len(docs)} documents, {len(paras)} paragraphs\n")
    print(f"chars/token   mean {mean:.2f}   min {min(docs):.2f}   max {max(docs):.2f}")
    print(f"CHARS_PER_TOKEN = {CHARS_PER_TOKEN}  "
          f"({'ok' if abs(mean - CHARS_PER_TOKEN) < 0.25 else 'DRIFTED — retune'})\n")
    print(f"estimate() error   mean {statistics.mean(errors) * 100:+6.1f}%   "
          f"p5 {errors[len(errors) // 20] * 100:+6.1f}%   "
          f"p95 {errors[len(errors) * 19 // 20] * 100:+6.1f}%")
