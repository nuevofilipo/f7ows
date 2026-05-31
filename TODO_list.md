**Phase 1: Railway Setup**

- [x] Create Railway project, spin up Postgres and Redis services
- [x] Create Railway Bucket, note down the credentials
- [x] Enable `pgvector` and `ltree` extensions on Postgres

**Phase 2: Python Backend Skeleton**

- [x] Init FastAPI project, set up `asyncpg` for raw async SQL
- [x] Write schema as plain SQL migration files via Alembic
- [x] Define tables: Users, Projects, Documents, Chunks (pgvector + ltree + UMAP coords + status as JSONB where flexible), Messages (ltree path), Artifacts
- [x] Run first Alembic migration against Railway Postgres
- [x] Set up ARQ + Redis connection

**Phase 3: Auth**

- [x] Registration + login endpoints
- [x] JWT issued as HTTP-only secure cookie -> !!! need to flip COOKIE_SECURE=true inside .env for production, over localhost secure doesn't work because not HTTPS
- [x] FastAPI dependency that authenticates every protected route

**Phase 4: Ingestion Pipeline**

- [x] Upload endpoint → save to Railway Bucket via boto3 → push ARQ job
- [x] ARQ worker: LlamaParse → chunk → embed → save to Postgres → UMAP → global doc summary

**Phase 5: Chat & LangGraph**

- [ ] ltree branch query (fetch ancestors for context window)
- [ ] LangGraph router: pgvector search vs global summary (not using langgraph after all, it would have been overkill)
- [ ] Streaming SSE response
- [ ] On chunk citation → mark chunk status as `studied`

**Phase 6: SvelteKit Frontend**

- [ ] Init SvelteKit + Tailwind, set up auth state
- [ ] 3-pane layout (Sources / Chat / Artifacts)
- [ ] Nested chat UI with Reply branching
- [ ] KaTeX rendering via rehype-katex + remark-math

**Phase 7: Slash Commands + Voice**

- [ ] `/deepsummary` map-reduce LangGraph graph
- [ ] `/mockexam` state machine + collapsible answer UI
- [ ] MediaRecorder in SvelteKit → Deepgram endpoint with custom vocab → LangGraph grader

**Phase 8: Coverage Map + Exports**

- [ ] 2D canvas chunk projection, colored by status
- [ ] Anki `.apkg` export via `genanki` → upload to Bucket → return download link
