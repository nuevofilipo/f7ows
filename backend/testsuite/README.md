# Transparent testsuite

Drives the **real** backend over HTTP — exactly like the frontend will — so the
tracer embedded in the backend (`app/trace.py`) drops human-readable artifacts
into `backend/runs/` for every call. Same traces whether triggered from here or
from a browser.

## Prerequisites

- `backend/.env` filled in and **Postgres / Redis / the bucket reachable**.
- Dependencies installed (`pip install -r requirements.txt`) — adds `httpx`,
  `uvicorn`, `arq` which the driver spawns.
- Run everything **from the `backend/` directory** (so `app` and `testsuite`
  import).

## Usage

```bash
# 1. Drop one or more PDFs here:
backend/testsuite/pdfs_to_upload/

# 2. Ingest them (auto-starts uvicorn + arq, uploads, waits for the worker):
python -m testsuite ingest

# 3. Chat with the material (terminal REPL):
python -m testsuite chat

# 4. Wipe the test project when you want a clean slate:
python -m testsuite clean          # data only
python -m testsuite clean --runs   # data + the runs/ trace folder
```

The driver creates a dedicated test user (`testsuite@local.test`) and project
(`testsuite`); your real data is never touched.

## REPL commands

`/intent specific|global|auto`, `/scope auto|topic|all`, `/budget <n>`,
`/k <n>` — flip retrieval knobs live. `/reply <ltree_path>` branches your next
message (nested chat). `/mockexam <topic>`, `/deepsummary <topic>` — exercise the
slash commands. `/new` new chat, `/knobs` show state, `/help`, `/quit`.

After each turn it prints the trace dir.

## What lands in `runs/`

- **ingest run** (`runN_ingest_<name>/`): `00_parsed.md`, `01_chunked.md`
  (full vs sparse per chunk), `02_embedded.md`, `meta.json` (per-stage timings).
- **chat run** (`runN_chat_<cmd>/`): `preferences.md` (every knob + the system
  prompt verbatim), `retrieval.md` (strategy + why + the assembled context),
  `prompt.md` (the exact messages sent to the LLM), `answer.md`, `meta.json`.
- **deepsummary run** (`runN_deepsummary_<topic>/`): `pass1_outline.md`,
  `pass2_*.md` per section, `final.md`.
- **project artifacts** (`project_<id8>/`): `clusters.md`, `projections.md`.

Tracing is on by default (`TRACE=1`); set `TRACE=0` to disable, or `TRACE_DIR`
to relocate the folder.
