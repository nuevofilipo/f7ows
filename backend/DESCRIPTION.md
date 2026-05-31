# Backend Description

A STEM-focused NotebookLM alternative. FastAPI web API + an ARQ background worker,
sharing Postgres (pgvector + ltree), Redis (the job queue), and an S3-compatible
bucket. Embeddings/chat go through OpenRouter. The web process stays light; all
heavy work (parsing, embedding, dim-reduction, clustering) happens in the worker.

## Two processes, one set of backing services

```
            HTTP                         enqueue (Redis)
  client ───────▶  FastAPI (app.main) ──────────────────▶  Redis  ──▶  ARQ worker (app.worker)
                       │  asyncpg          │                                  │
                       └──── Postgres ◀────┴──── bucket (S3) ────────────────┘
```

- **Web** (`uvicorn app.main:app`): auth, projects, document upload. Validates,
  stores the PDF, inserts rows, enqueues jobs, returns immediately.
- **Worker** (`arq app.worker.WorkerSettings`): runs the ingest pipeline and the
  project-wide recompute jobs. No HTTP.

They never call each other directly — the web side **enqueues**, the worker
**consumes**.

## Infrastructure modules (app/)

- **config.py** — `settings`, a frozen dataclass loaded once from env (`.env`):
  Postgres/Redis URLs, the 5 bucket vars, JWT/cookie config, CORS, and the
  OpenRouter key + embed/LLM base-URLs and models.
- **db.py** — the asyncpg pool. `create_pool()/close_pool()` (lifespan + worker
  startup), `acquire()` async-context for a connection. On each connection it
  registers the pgvector codec so `halfvec` columns accept/return Python lists.
- **jobs.py** — the **producer** side of ARQ (web only). Holds an `ArqRedis` pool;
  `enqueue_ingest(document_id)` and `enqueue_finalize(project_id, document_ids)`
  push jobs onto Redis.
- **main.py** — builds the FastAPI app: lifespan opens/closes the DB + Redis
  pools, adds CORS, and `include_router`s auth, projects, documents. `/healthz`.
- **auth.py** — `/auth` router (register/login/logout/me). Argon2 password hashes,
  JWT in an HTTP-only cookie. `get_current_user` is the dependency every protected
  route uses; returns the user dict.
- **llm.py** — thin async OpenRouter chat helper. `complete(messages, model=...)
  -> str`. Used by the retrieval planner; reusable by future chat features.

## The library (app/library/) — sources & their web API

- **storage.py** — async wrapper over the bucket (boto3 in a threadpool, one
  cached client). `upload_bytes(key, data, ct)`, `download_bytes(key) -> bytes`.
  Shared by web (upload) and worker (download).
- **projects.py** — `/projects` router. `POST` (create) / `GET` (list), scoped to
  `get_current_user`. A project owns documents, chunks, clusters.
- **documents.py** — `/projects/{id}/documents` router. `_store_and_enqueue(file)`
  validates PDF + size, uploads to the bucket key
  `documents/{project}/{document}/{name}`, inserts a `documents` row
  (`status={"state":"uploaded"}`), and `enqueue_ingest`s it. `POST ""` (single),
  `POST "/batch"` (many, partial-failure tolerant), `GET ""` (list). After
  upload(s) it calls `enqueue_finalize` once.

## The pipeline stages (each a self-contained package, hidden behind one function)

- **ingest/parse.py** — `parse(pdf_bytes, *, use_models=False) -> markdown`.
  Default fast path: pypdfium2 native-text extraction + beamer overlay-page dedup
  (no ML, sub-second). Opt-in model path: full Docling (layout/table/formula→LaTeX),
  slow. Output is structural Markdown.
- **chunk/chunk.py** — `chunk(markdown, *, doc_slug) -> [Chunk]`. Splits on
  Markdown headings into a heading-stack, slugifies each into an **ltree path**
  (sibling-deduped), atomizes bodies (keeping `$$…$$` equations indivisible), and
  packs atoms to a token target (MIN/MAX). The boundary level is *measured*
  (`_pick_pivot`) — the heading depth whose section sizes sit closest to target,
  with a flat fallback. Also emits `content_sparse` (regex+stopword compression
  that keeps headings/LaTeX/bold/bullets). `Chunk` = (index, content_full,
  content_sparse, path, heading_trail, token_count).
- **embed/embed.py** — `embed(texts) -> [vector]`. Batched OpenRouter call to
  `text-embedding-3-large` (3072-d), retries on 429/5xx, oversized inputs
  token-truncated. Returns one vector per input, in order.
- **projections/projections.py** — `compute(vectors) -> {umap_2d, umap_3d, pca,
  tsne}`. Reduces the project's whole embedding matrix to display coordinates
  (each (N,k) array or None if too few points). Deterministic.
- **clusters/clusters.py** — `compute_clusters(vectors, sparse_texts) ->
  [ClusterResult]`. Mid-dim UMAP → HDBSCAN (auto cluster count), then per cluster:
  centroid (mean, original space), medoid (member nearest centroid), TF-IDF label
  (clean ASCII words). Single level for now (`clusters.parent_id/level` reserved
  for recursion).

## The worker jobs (app/worker.py)

`WorkerSettings` registers the jobs; `startup` gives the worker its own DB pool in
`ctx`; `ctx["redis"]` lets a job enqueue follow-ups.

- **ingest_document(document_id)** — the per-document pipeline. Loads the row →
  status `parsing` → `storage.download_bytes` → `parse` → `chunk` → status
  `embedding` → `embed(content_full per chunk)` → transactional DELETE+INSERT of
  `chunks` (content_full, content_sparse, embedding, path::ltree, metadata) →
  status `ready`. `parse`/`chunk` run in a threadpool. On error: status `error`,
  re-raise. It does **not** trigger projections/clusters (see finalize).
- **finalize_project(project_id, document_ids, attempts_left)** — coalesces the
  project-wide recompute so a batch recomputes **once, after the last doc**. Polls
  whether all `document_ids` are terminal (ready/error); if not, re-enqueues
  itself deferred 5s (cooperative wait); once done, enqueues exactly one
  `recalculate_projections` + one `recalculate_clusters`.
- **recalculate_projections(project_id)** — load all project chunk embeddings →
  `projections.compute` (threadpool) → write `umap_2d/umap_3d/pca_coords/
  tsne_coords` (JSONB) per chunk. Project-wide: a new doc moves the whole map.
- **recalculate_clusters(project_id)** — load embeddings + `content_sparse` →
  `clusters.compute_clusters` → replace the project's `clusters` rows (centroid,
  medoid_chunk_id, label, member_count).
- **generate_apkg(artifact_id)** — stub (Anki export, later).

## Retrieval (app/retrieval/router.py) — the Dynamic Sparse Router

`route(project_id, query, *, intent, scope, k, budget) -> RetrievalResult`. Picks
the cheapest strategy that answers, by token budget. It only **gathers** context
(text + source chunks); it does not call the answering LLM.

- **specific** — pgvector top-k cosine (`embedding <=> $q::halfvec`) over
  `content_full`.
- **stuff** (global, corpus ≤ budget) — all `content_sparse`, ltree-ordered.
- **skeleton** (global, large, topic) — fetch the `DISTINCT path` ToC, the cheap
  `llm.complete` planner selects branches, fetch `content_sparse WHERE path <@
  ANY(selected)`; falls back to medoid on LLM/parse failure.
- **medoid** (global, large, "everything") — `content_sparse` of each cluster's
  medoid; full thematic coverage for almost no tokens.

Corpus size is estimated cheaply via `SUM(length(content_sparse))/4` so a huge
project is never loaded just to decide not to. `intent`/`scope` default to keyword
heuristics but are meant to be passed explicitly (a slash command is unambiguous).

## End-to-end data flow (upload → queryable)

1. `POST /projects/{id}/documents[/batch]` → bucket save + `documents` row
   (`uploaded`) + `enqueue_ingest` per file + one `enqueue_finalize`.
2. Worker `ingest_document` per file: download → parse → chunk → embed → write
   `chunks` → `ready`.
3. `finalize_project` waits for all of the batch, then triggers
   `recalculate_projections` + `recalculate_clusters` **once**.
4. Chunks now have embeddings, ltree paths, sparse text, display coords; the
   project has cluster nodes.
5. `route(project_id, query, …)` assembles context from those artifacts for the
   (future) answering/chat layer.

## Schema (alembic, migrations only — app uses raw asyncpg SQL)

`0001` users, projects, documents, chunks (halfvec(3072) embedding, ltree path,
projection JSONB cols, HNSW + GiST + FTS indexes), messages (ltree-threaded chat),
artifacts; extensions vector/ltree/pgcrypto. `0002` renames `chunks.content →
content_full`, adds `content_sparse`, and the `clusters` table (centroid,
medoid_chunk_id, parent_id/level for future recursion). Run with `alembic upgrade
head`; `alembic/env.py` derives a sync psycopg URL from `POSTGRES_URL`.
