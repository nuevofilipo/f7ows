"""
Projection — reduce chunk embeddings to display coordinates for the map UI.

A single pure function, ``compute(vectors) -> dict``, takes the project's full
(N, 3072) embedding matrix and returns 2D/3D layouts:

    {"umap_2d": (N,2), "umap_3d": (N,3), "pca": (N,2), "tsne": (N,2)}

Each value is a numpy array aligned to the input rows, or ``None`` if that method
couldn't run (too few points). The worker maps these back onto chunk ids and
stores them as JSONB. This must run over *all* of a project's chunks at once —
manifold methods place each point relative to the whole cloud, so a single point
has no meaningful position (PHASE4_PLAN_v2 §1 / PRD).

Heavy + imports are slow (umap-learn pulls in numba), so everything is imported
lazily inside the function — importing this module stays cheap for the worker.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SEED = 42                # deterministic layouts so the UI doesn't reshuffle
_MIN_FOR_MANIFOLD = 4     # UMAP / t-SNE need a small neighbourhood to be meaningful


def compute(vectors) -> dict:
    """Reduce an (N, D) embedding matrix to display coordinates. See module doc."""
    import numpy as np

    vectors = np.asarray(vectors, dtype=np.float32)
    n = len(vectors)
    out: dict = {"umap_2d": None, "umap_3d": None, "pca": None, "tsne": None}
    if n < 2:
        return out  # nothing to lay out

    # PCA — cheap, linear, always works for N >= 2.
    try:
        from sklearn.decomposition import PCA

        out["pca"] = PCA(n_components=2, random_state=_SEED).fit_transform(vectors)
    except Exception:  # noqa: BLE001
        logger.warning("projections: PCA failed", exc_info=True)

    if n < _MIN_FOR_MANIFOLD:
        # Too few points for UMAP/t-SNE to be stable; PCA (if any) is enough.
        return out

    # UMAP 2D + 3D — neighbourhood capped below N.
    try:
        from umap import UMAP

        n_neighbors = min(15, n - 1)
        for key, dims in (("umap_2d", 2), ("umap_3d", 3)):
            out[key] = UMAP(
                n_components=dims, n_neighbors=n_neighbors,
                min_dist=0.1, metric="cosine", random_state=_SEED,
            ).fit_transform(vectors)
    except Exception:  # noqa: BLE001
        logger.warning("projections: UMAP failed", exc_info=True)

    # t-SNE 2D — perplexity must be < N.
    try:
        from sklearn.manifold import TSNE

        perplexity = min(30.0, max(2.0, (n - 1) / 3))
        out["tsne"] = TSNE(
            n_components=2, perplexity=perplexity,
            init="pca", random_state=_SEED,
        ).fit_transform(vectors)
    except Exception:  # noqa: BLE001
        logger.warning("projections: t-SNE failed", exc_info=True)

    return out
