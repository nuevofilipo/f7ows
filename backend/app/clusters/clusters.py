"""
Clustering — group a project's chunks into semantic clusters for the global-query
router and the map UI.

One pure function, ``compute_clusters(vectors, sparse_texts) -> list[ClusterResult]``.
Single level for now (the `clusters` table keeps parent_id/level so this can grow
into a recursive tree later without schema changes). Per PHASE4_PLAN_v2 §2.C each
cluster carries:

* **centroid** — mean embedding, in the original (un-reduced) space so it's directly
  comparable to query vectors;
* **medoid** — the real member chunk closest to that centroid (a concrete,
  quotable representative);
* **label** — a cheap TF-IDF summary over the members' sparse text.

We cluster in a mid-dim UMAP space (density methods are unstable in 3072-d, §2.C),
but compute centroid/medoid back in the full space. HDBSCAN picks the number of
clusters itself; points it calls noise are dropped, and if it finds nothing we
fall back to a single cluster so the project always has at least one node.

Heavy imports (umap/numba) are deferred into the function to keep import cheap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_MID_DIM = 10            # mid-dim target for the clustering space (§2.C: ~10-50d)
_MIN_CLUSTER_SIZE = 2    # smallest group HDBSCAN may call a cluster
_MIN_FOR_REDUCE = 4      # below this, skip UMAP and treat everything as one cluster
_LABEL_TERMS = 4         # TF-IDF terms per cluster label

# Only ASCII word tokens of 3+ letters. The trailing \b (unicode-aware) means a
# fragment with a glyph stuck to it — "logpθ", "pθ", "ˆyθ" from the plain-text
# equations — has no word boundary and is rejected outright, so labels come out
# as real words. Numbers and 1-2 letter noise are dropped too.
_LABEL_TOKEN_RE = r"(?u)\b[a-z]{3,}\b"
# Common math/LaTeX words that survive as clean ASCII but make poor labels.
_MATH_STOPWORDS = frozenset("""
log exp sum max min arg argmax argmin softmax sqrt frac cdot leq geq
theta phi psi alpha beta gamma delta lambda sigma mu epsilon nabla
mathbb mathcal mathrm mathbf text left right partial forall exists
""".split())


@dataclass
class ClusterResult:
    member_indices: list[int]   # rows of the input matrix in this cluster
    medoid_index: int           # the representative member (index into the input)
    centroid: list[float]       # mean embedding, original space
    label: str                  # TF-IDF summary
    member_count: int


def _tfidf_labels(cluster_docs: list[str]) -> list[str]:
    """A short, discriminative label per cluster — top TF-IDF terms over each
    cluster's concatenated member text. Falls back to 'cluster N' if the text is
    too thin to vectorise."""
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer

    stop_words = list(ENGLISH_STOP_WORDS | _MATH_STOPWORDS)
    try:
        vec = TfidfVectorizer(
            stop_words=stop_words, token_pattern=_LABEL_TOKEN_RE, max_features=2000,
        )
        matrix = vec.fit_transform(cluster_docs)
        terms = vec.get_feature_names_out()
        labels = []
        for i in range(matrix.shape[0]):
            row = matrix.getrow(i).toarray().ravel()
            top = [terms[j] for j in row.argsort()[::-1][:_LABEL_TERMS] if row[j] > 0]
            labels.append(", ".join(top) if top else f"cluster {i + 1}")
        return labels
    except Exception:  # noqa: BLE001 - labels are best-effort
        logger.warning("clusters: TF-IDF labelling failed", exc_info=True)
        return [f"cluster {i + 1}" for i in range(len(cluster_docs))]


def compute_clusters(vectors, sparse_texts: list[str]) -> list[ClusterResult]:
    """Cluster ``vectors`` (N, 3072) and summarise each group. See module doc."""
    import numpy as np

    vectors = np.asarray(vectors, dtype=np.float32)
    n = len(vectors)
    if n == 0:
        return []

    # Normalised copy: cosine geometry for medoid scoring (and UMAP uses cosine).
    units = vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)

    if n < _MIN_FOR_REDUCE:
        labels = np.zeros(n, dtype=int)  # too few points to cluster meaningfully
    else:
        from sklearn.cluster import HDBSCAN
        from umap import UMAP

        dim = min(_MID_DIM, n - 2)
        reduced = UMAP(
            n_components=dim, n_neighbors=min(15, n - 1),
            metric="cosine", random_state=42,
        ).fit_transform(vectors)
        labels = HDBSCAN(min_cluster_size=_MIN_CLUSTER_SIZE).fit_predict(reduced)
        if not (labels >= 0).any():
            logger.info("clusters: HDBSCAN found only noise; using a single cluster")
            labels = np.zeros(n, dtype=int)

    groups = [np.where(labels == lbl)[0] for lbl in sorted(set(labels)) if lbl != -1]
    cluster_docs = [" ".join(sparse_texts[i] for i in idx) for idx in groups]
    label_texts = _tfidf_labels(cluster_docs) if cluster_docs else []

    results: list[ClusterResult] = []
    for idx, label in zip(groups, label_texts):
        centroid = vectors[idx].mean(axis=0)
        centroid_unit = centroid / (np.linalg.norm(centroid) + 1e-9)
        # Medoid = member with the highest cosine similarity to the centroid.
        medoid_local = idx[int(np.argmax(units[idx] @ centroid_unit))]
        results.append(ClusterResult(
            member_indices=idx.tolist(),
            medoid_index=int(medoid_local),
            centroid=centroid.tolist(),
            label=label,
            member_count=len(idx),
        ))
    return results
