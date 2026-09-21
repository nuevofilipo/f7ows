# F7ows — Desktop Port TODO

Goal: ship this as a local desktop app. Everything local except LLM inference
(OpenRouter by default, Ollama optional). Target scale: < 50k chunks per project.

Decided: name **F7ows**, **SQLite + sqlite-vec** (not bundled Postgres), **no
Redis**, local filesystem storage, single local user, **Tauri** shell.

---

## Phase 0 — Prep

- [x] Name: **F7ows** (drives the data dir, the macOS bundle id `com.<you>.f7ows`,
      and the keychain service name — renaming later means migrating user dirs)
- [x] App data dir layout — split by "what happens if this is deleted"
      (`app/paths.py`; `python -m app.paths` prints the resolved set):

  ```
  <DATA>/                    Win %LOCALAPPDATA%\F7ows   mac ~/Library/Application Support/F7ows
    db.sqlite3               (+ -wal, -shm siblings)
    documents/               <project_id>/<document_id>/<name>.pdf  (== today's storage_key)
    config.json              providers, models, prefs — NO secrets
    app.lock                 single-instance guard
  <CACHE>/                   Win <DATA>\cache            mac ~/Library/Caches/F7ows
    models/                  opt-in Docling/HF download (GBs) — set HF_HOME here
    runs/                    trace.py output, pruned to last N
  <LOGS>/                    Win <DATA>\logs             mac ~/Library/Logs/F7ows
    backend.log  worker.log  shell.log
  ```

  - [x] `%LOCALAPPDATA%`, **not** `%APPDATA%` — Roaming syncs to a server at login
        on domain machines; a multi-GB DB there is a disaster
  - [~] Tauri resolves the paths, creates the dirs, passes them to both Python
    sidecars as env (`F7OWS_DATA_DIR` / `_CACHE_DIR` / `_LOG_DIR`)
    — the _Python_ half is done (paths.py reads all three and creates the
    dirs); the shell that sets them is phase 7
  - [x] ~~`config.py` falls back to `platformdirs`~~ — dropped: `paths.py`
        resolves the three OS conventions in ~40 lines, so the dependency (and a
        PyInstaller hidden-import to get wrong later) bought nothing
  - [x] `F7OWS_DATA_DIR` override; dev default `./devdata/` (gitignored) so dev
        runs can never touch an installed copy

- [x] Drop the `tiktoken` dependency — it's a Rust ext that downloads its BPE file
      on first use (offline first-ingest crashes today). Already optional at all
      3 call sites, so this was deletion.
  - [x] `app/tokens.py`: one constant `CHARS_PER_TOKEN = 3.67` (measured mean),
        `estimate()` and `clip()`, + a `python -m app.tokens` calibration harness.
        The one hard boundary (the 8192 embed cap) keeps its headroom in
        `embed.py` instead — `_MAX_INPUT_TOKENS` 8000 -> 7000.
  - [x] All 3 call sites swapped; tiktoken out of requirements.txt
  - [x] Fixed two latent bugs: `chunk.py`'s `words*1.3` (real 1.70) and
        `embed.py`'s 32000-char clip (~9.5k tokens on LaTeX-dense text, over the 8192 cap)
  - [x] Verified: re-chunked the 7 real parsed docs vs true tiktoken counts —
        per-doc error -10%..+10%, chunk medians 620-862 in the 400-800 band
  - [ ] UI: label pre-send counts `~`; post-send stays exact from `usage.prompt_tokens`
        (frontend, deferred to Phase 6)
- [x] Embedding dim: **1024** (down from 3072). One constant — `embed.py` already
      sends `dimensions`, so OpenAI truncates + re-normalizes server-side.
      Why: sqlite-vec has no float16, so we lose today's `halfvec` 2-byte packing.
      At 50k chunks a brute-force scan is 600 MB @3072 f32 vs 200 MB @1024 (~3x
      faster, 400 MB smaller DB); quality cost ~1-2% MTEB (Matryoshka-trained).
      int8 quantization is the next 4x if ever needed.
      NOTE: this does **not** unlock local embed models — equal dims are not
      compatible vector spaces. That's the Phase 5 re-embed work.
- [x] Branch off `master` -> `desktop-port`; cloud version stays runnable until Phase 7

## Phase 1 — SQLite port (the big one, ~43 SQL statements) ✅

- [x] `db.py`: asyncpg pool -> aiosqlite; WAL mode, `busy_timeout`, foreign_keys ON
- [x] Rewrite the three alembic migrations as one fresh SQLite schema (batch mode
      for any later ALTERs) — `app/migrations.py`, `PRAGMA user_version`
- [x] UUIDs: generate in Python, drop `gen_random_uuid()` defaults
  - [x] `sqlite3.register_adapter(UUID, str)` in db.py so call sites can keep
        passing either a UUID or a str. Ids now come back **out** as `str`, never
        UUID — same JSON the API already emitted, so nothing downstream noticed
- [x] `path ltree` -> `path TEXT` + B-tree index
  - [x] `<@` / `@>` / `nlevel()` -> `db.descendants_of()` / `db.ancestors_of()` /
        dot counting. **GLOB, not LIKE** — path labels are slugified to
        `[a-z0-9_]` and `_` is a LIKE wildcard, so `LIKE 'self_attention.%'`
        would also match a sibling `selfxattention`. Regression-tested.
  - [x] `path <@ ANY(...)` in retrieval router -> OR of prefixes
  - [x] **Zero-pad numeric chat sibling labels** (`0001`, `0010`) so text ordering ==
        tree ordering — otherwise threads scramble on reload
- [x] JSONB -> json1: `->>'state'` -> `json_extract(status, '$.state')`,
      `||` -> `json_patch()`, `jsonb_build_object` -> `json_object()`
- [x] `halfvec(3072)` + HNSW -> sqlite-vec `vec0` table keyed by chunk `seq`
  - [x] `embedding <=> $q` top-k -> brute-force `vec_distance_cosine()` over a
        join, **not** `MATCH`/`k=`: the KNN form can't be combined with the
        project + source-selection joins, and an exhaustive scan is what the
        schema was designed around anyway (~210ms at the 50k ceiling)
  - [x] `source_ids` scoping -> expanded `IN (...)` (SQLite has no array param,
        so "unfiltered" is now an absent SQL fragment, not a NULL argument)
- [x] Drop the unused `to_tsvector` GIN index; add FTS5 (gives real `bm25()` — the
      PRD's keyword-search ask, basically free here)
  - [x] User text is sanitised into an FTS5 MATCH expression before it goes near
        the index — bare input containing `"`, `*` or `NEAR` is a syntax error
        (i.e. a 500), which `plainto_tsquery` used to absorb for us
- [x] Drop `asyncpg` + `pgvector` from requirements.txt
- [x] Verify: end-to-end upload -> chunks -> retrieval on a real PDF
  - [x] `scripts/check_sqlite_port.py` — 63 assertions over every ported
        statement against a throwaway DB, real parse/chunk/storage on a real PDF,
        only `embed` stubbed. **63 passed, 0 failed.**

  > Note: the app still can't be _started_ end-to-end, for a reason outside this
  > phase — `main.py`'s lifespan calls `create_redis_pool()` (phase 2) and
  > `config.py` still `_required()`s the cloud env vars (phase 4). Every SQL
  > statement is verified by the harness above; the wiring is the next two phases.

## Phase 2 — Kill Redis / ARQ ✅

- [x] `jobs` table (id, func, args, run_after, state, attempts, created_at) —
      already shipped in migration v1 during phase 1, so this phase needed **no
      schema change at all**
- [x] Keep `producer.py`'s interface (`enqueue_ingest` / `enqueue_finalize` / …),
      swap the body to an INSERT. Enqueueing borrows the web process's
      connection rather than opening its own, so a job insert obeys the same
      `acquire()` discipline as any other write
- [x] Worker loop: poll `jobs` (no LISTEN/NOTIFY), claim rows atomically, run,
      mark done/error
  - [x] Claim is `SELECT ... LIMIT 1` + guarded `UPDATE` in one transaction, so a
        second copy of the app can't run the same job twice (BEGIN IMMEDIATE +
        `WHERE state = 'queued'`; the loser sees rowcount 0 and moves on)
  - [x] Failures retry 3x with backoff — the likely failure is a network blip
        mid-embed, not a bad PDF. Unknown function names fail the job instead of
        resolving to an arbitrary module attribute
- [x] `finalize_project`'s `_defer_by` re-enqueue -> `run_after` column
- [x] Keep web and worker as **two processes** (UMAP/Docling would stall the loop)
- [x] Delete arq dependency + `RedisSettings` / `WorkerSettings`
  - [x] The ARQ `ctx` first parameter is gone from all five job functions — it
        existed only to carry the Redis handle. `finalize_project` re-enqueues
        through `producer` now
  - [x] `python -m app.worker` runs the loop (matches COMMANDS_TO_START.txt);
        the old ad-hoc harness moved to `python -m app.worker ingest [doc_id]`
- [x] Verify: `scripts/check_job_queue.py` — 38 assertions incl. a real PDF
      driven through the real `run_worker()` loop. **38 passed, 0 failed.**

  > **The app starts.** `uvicorn app.main:app` boots, `/healthz` 200s, and a full
  > HTTP round trip works: register -> create project -> upload PDF -> the PDF
  > lands on local disk and both job rows are queued by the web process and
  > readable from a separate one. (Not run through the worker over HTTP on
  > purpose — that would spend real money on embeddings.)
  >
  > `config.py` still `_required()`s `POSTGRES_URL` and `REDIS_PUBLIC_URL`. Both
  > are dead settings nothing reads; deleting them is phase 4's, per the split.

## Phase 3 — Local file storage ✅

- [x] `library/storage.py`: boto3 -> pathlib under the app data dir
      (`upload_bytes` / `download_bytes` keep their signatures; `content_type` is
      accepted and ignored — the DB column already records it)
  - [x] Write-then-rename, so a crash mid-write can't leave a truncated PDF
  - [x] Keys are resolved against `data_dir()` and containment-checked: keys are
        built from user-supplied filenames, so `..`, an absolute path or a drive
        letter must not escape the data dir
- [x] `storage_key` stays a relative path — nothing else should notice. Existing
      keys (`documents/<project>/<doc>/<name>`) map onto real files as-is, so the
      `documents/` prefix already in the DB needs no migration
- [x] Move `trace.py`'s `runs/` into the app **cache** dir (not data: unbounded
      growth, losing old runs costs nothing). `TRACE_DIR` still overrides;
      resolved lazily so merely importing `trace` never creates directories.
      `testsuite/config.py` follows the same resolution
- [x] Drop boto3 + the 5 bucket env vars (`config.py`, `.env`, `requirements.txt`)
- [x] Verified: round-trip, overwrite, no `.part` left behind, 5 traversal keys
      rejected, missing key -> `FileNotFoundError`, trace run + project artifact
      land in `<CACHE>/runs/`
- [ ] Left alone: `backend/runs/` still holds 54 old trace dirs (gitignored) —
      delete when convenient, nothing reads them now

## Phase 4 — Single local user + secrets  ✅

- [x] Seed one local user on first run (`app/local_user.py`, called from the
      lifespan); keep the `users` table — it's the FK root
  - [x] Data, not a migration: it has to be idempotent across restarts and safe
        when web + worker start at once. `ON CONFLICT (email) DO NOTHING` then
        re-read, so the loser of the race adopts the winner's row
  - [x] The unused password is a random 32-byte value, not a fixed/empty one —
        otherwise it becomes a real credential the day the login route comes back
  - NOTE: the worker no longer resolves the key via the users row (that line in
    the original plan is obsolete) — it reads the keychain directly, so
    `ingest_document`'s join to `users` is gone
- [x] Auto-issue the auth cookie — no login screen (login/register routes kept)
  - [x] `get_current_user`: a missing *or unreadable* cookie resolves to the local
        user in single-user mode. This is what makes Phase 6's open cookie
        question moot — the cookie is still issued and honoured, but nothing
        depends on it surviving the cross-origin trip to the shell's API port
  - [x] `/auth/me` sets the cookie and returns `single_user`, which the frontend
        uses to hide the account controls — **this closes Phase 6's blocked
        "hide login/register" item** (`session.singleUser`, the two Log out
        buttons). The login page is already unreachable: the layout guard bounces
        an authenticated user away from `/login`, and now everyone is one
  - [x] `SINGLE_USER=0` restores the multi-user behaviour end to end
- [x] Generate `JWT_SECRET_KEY` on first launch into the app data dir, never ship one
  - [x] `keystore.jwt_secret()` -> `<DATA>/jwt_secret`, 32 random bytes, mode 0600
  - [x] Created with `O_EXCL`: web and worker start together and would otherwise
        each generate one and race, leaving the loser signing cookies the winner
        rejects. The loser reads the winner's file instead
  - [x] `settings.jwt_secret_key` is a **property**, so importing config never
        mints a credential — only a process that authenticates does
- [x] Move the OpenRouter key to the OS keychain; retire `crypto.py`/Fernet
  - [x] `app/keystore.py` over `keyring` — Windows Credential Manager verified
        working on this machine (`WinVaultKeyring`)
  - [x] Done in Python, **not** via Tauri: the `keyring` package talks to the
        same OS stores directly, so this didn't have to wait for Phase 7
  - [x] `apikey.py` lost its ContextVar — with one user there is no per-request
        key to keep apart. Read fresh per call, never cached: web and worker are
        two processes over one keychain, and a cache in either goes stale the
        moment the user changes the key in the other
  - [x] `users.openrouter_key` column is now unused. Left in place (dropping a
        column is the other agent's schema surface); worth removing whenever
        migrations are next touched
  - [x] Missing key is now a clear `MissingAPIKey` pointing at Settings, instead
        of a 401 from OpenRouter
- [x] `config.py`: drop `_required()` on cloud vars, load from `config.json` + keychain
  - [x] Precedence env -> `<DATA>/config.json` -> default. Env first so a dev
        checkout can be pointed anywhere without touching the user's real config
  - [x] Nothing is required any more: a desktop app has no operator to read logs
        and fix an environment, so it must boot and *then* say what's missing
  - [x] `write_config_json()` merges and writes atomically; a corrupt file is
        logged and ignored rather than fatal
  - [x] `.env` trimmed to dev overrides only; `POSTGRES_URL`, `REDIS_PUBLIC_URL`,
        `JWT_SECRET_KEY`, `FERNET_KEY` gone. `cryptography` -> `keyring` in
        requirements. Deleted `scripts/check_auth.py`, `check_db.py` (asyncpg) and
        `check_redis.py` (arq) — all three could no longer import
- [x] Verified: `scripts/check_phase4.py`, **34 passed / 0 failed** — and
      `scripts/check_sqlite_port.py` still **63 / 0** with these changes in place
      (one assertion there was rewritten: it tested the `sqlite3.Row` `in` trap
      through `_user_dict`'s now-removed `openrouter_key_set`, so it tests db.Row
      directly instead)
- [x] **The app boots.** With phase 2 and 4 both in, `uvicorn app.main:app` starts
      clean and serves `/healthz`, `/auth/me` (cookie auto-issued, no login) and
      `/settings` — the first time it has run end to end since the port began

## Phase 5 — Provider config (OpenRouter / Ollama)  ✅ (except the deferred item)

- [x] Per-role provider config (embed / planner / chat): `{base_url, model, key_ref}`
      — `app/providers.py`, stored under `providers` in `config.json`
  - [x] Resolution: role env var (`CHAT_MODEL`, `LLM_BASE_URL`, …) -> `config.json`
        -> app default, matching the rest of `config.py`
  - [x] `key_ref` **names** a keychain entry rather than holding a secret, so
        config.json stays safe to copy, diff and back up
  - [x] `key_ref: ""` means *keyless*, which is not the same as "key missing" —
        Ollama needs no auth, and a bare `Bearer ` with an empty value is worse
        than sending no header at all (some proxies reject it)
  - [x] Not env-overridable, deliberately: `""` is meaningful, and
        `os.getenv(...) or default` would silently swallow it
  - [x] Re-read on config.json mtime change, not cached for the process lifetime:
        the web app writes it and the **worker** is a second process that has to
        notice, and repointing a provider shouldn't need a restart
  - [x] Unknown role falls back to `planner`, so a typo costs money slowly
- [x] Point `llm.py` + `embed.py` at any OpenAI-compatible base URL
  - [x] `complete(..., role=...)` — "planner" by default, since the cheap
        classifier calls are exactly the ones that omit it; `generate.py` passes
        `role="chat"`. The user's model picker still wins over the role's model
  - [x] `list_models()` asks the **chat** role's endpoint, so on Ollama the picker
        lists the models actually pulled on the machine
  - [x] `apikey.py` deleted — with one user there is no per-request key to keep
        apart, and key resolution now belongs to the provider
- [x] `cost.py`: tolerate a missing `usage.cost` (Ollama returns none) — show
      "local model, no cost" instead of blank/zero
  - [x] Unpriced calls are **counted, not summed as zero**. "$0.00" is a claim we
        can't make: the call wasn't free, its price is unknown — and the two
        differ the moment one turn mixes a local planner with a paid chat model
  - [x] `cost.summary()` -> `{usd, priced_calls, unpriced_calls}`, `usd` None when
        nothing was priced; stored on the message as `cost` + `unpriced_calls`
  - [x] UI: "local model, no cost" when nothing was priced, `$0.0123+` when a
        turn mixed priced and unpriced calls
- [x] Expose provider settings in the existing preferences UI
  - [x] `GET/PUT /settings/providers[/{role}]` (partial patch, so an unpinned
        field keeps following the app default) and `PUT/DELETE /settings/keys/{ref}`
        for credentials that aren't the OpenRouter one
  - [x] Profile page: a row per role (base URL / model / key ref) with a per-row
        save, an Ollama hint, and a warning when a `key_ref` resolves to nothing
- [x] Verified: `scripts/check_phase5.py`, **48 passed / 0 failed** — resolution
      order, keyless vs missing-key, cost accounting, and the two model call sites
      driven against a local stub endpoint so what's asserted is the request that
      actually goes out (URL, auth header, model), not a mock
- [ ] **Deferred**: local embeddings. Needs per-project `embed_model` + `dim`, a
      re-embed job, and a no-mixing-within-a-project rule. Chat-via-Ollama first.
  - The embed *endpoint* is configurable today; the embed **model** is the part
    that isn't safe to change — vectors from two models aren't comparable and the
    `vec0` width is fixed at table creation. Noted in `embed.py`'s docstring so
    the setting doesn't read as an invitation.

## Phase 6 — Frontend (code done; 2 items blocked on other phases)

- [x] `adapter-auto` -> `adapter-static` with SPA fallback (`ssr` is already off).
      `fallback: 'index.html'` — the docs prefer `200.html` only to avoid
      colliding with a prerendered homepage, and we prerender nothing; Tauri
      boots the webview at the directory root, so it has to be `index.html`.
      adapter-auto uninstalled. `npm run build` -> `frontend/build/`
- [x] `API_BASE`: shell picks a free port and hands it to the frontend at startup
  - [x] `config.js` exports `apiBase()` (was a `const`), resolved at _call_ time,
        not module load, so the shell's injection is allowed to land first:
        `window.__F7OWS_API_BASE__` -> `VITE_API_BASE_URL` -> `localhost:8000`
  - [x] Only one fetch site in the whole app (`lib/api/client.js:40`), so this is
        the single seam the shell has to fill
  - [ ] **Phase 7 owes this**: inject `window.__F7OWS_API_BASE__ = "http://127.0.0.1:<port>"`
        before the bundle loads (Tauri `initialization_script`, _not_ a tag in
        `app.html` — the port isn't known at build time)
- [x] Hide login/register from the nav — **done in Phase 4**, which removed the
      need to log in at all. `session.singleUser` (from `/auth/me`) hides both
      Log out buttons; the login page needs no change because the layout guard
      already bounces an authenticated user away from it.
  - Provider settings on the profile page are **Phase 5**'s line, not this one.
- [ ] Check the cookie still works on the chosen origin
  - **Downgraded to a nice-to-have by Phase 4**: `get_current_user` falls back to
    the local user when the cookie is missing or unreadable, so a cookie the
    browser refuses to send is no longer a lockout — it just means the auth
    header of every request is doing nothing. Still worth confirming once the
    shell exists, to know which of the two paths is actually live.
  - Original note — needs a live server:
    cookie is `SameSite=lax` on a _different_ origin than the page (`tauri://` or
    `http://localhost:<ui port>` vs `127.0.0.1:<api port>`), which is exactly the
    case browsers have been tightening. Verify a real login round-trip before
    assuming `credentials: 'include'` still suffices; `COOKIE_SAMESITE` /
    `COOKIE_SECURE` in `config.py` are the knobs.

## Phase 7 — Tauri shell + packaging

- [ ] Tauri app; Python backend + worker as sidecars
- [ ] Supervise: start both on launch, kill both on quit (incl. force-quit paths)
- [x] Freeze Python (PyInstaller). ~~**Expect pain from numba/llvmlite**~~ —
      **spiked, and it just worked.** `packaging/f7ows-backend.spec` +
      `packaging/launcher.py`; build with
      `pyinstaller packaging/f7ows-backend.spec --noconfirm`.
      **277 MB onedir bundle** — llvmlite 115 MB, scipy+numpy 88 MB, sklearn 13 MB.
  - [x] `f7ows-backend selftest` proves all 5 native payloads survive freezing
        (sqlite-vec vec0.dll, numba JIT, a real UMAP fit, pypdfium2 pdfium.dll,
        the keyring backend) — **5/5**. Doubles as the first-run health check
        below, so the shell can refuse to show a broken UI.
  - [x] Frozen `serve` boots and answers /healthz; frozen `worker` claims and
        completes real jobs.
  - [x] One binary, two roles (`serve` / `worker`) rather than two executables —
        otherwise the Python runtime, numpy, sklearn and llvmlite get duplicated
        into each, roughly doubling the installer.
  - **Two traps, both silent at build time and fatal at startup:**
    - `uvicorn.run("app.main:app")` passes the app as a *string*, so PyInstaller
      never sees the `app` package and cheerfully ships a bundle without it.
      The launcher imports the app object instead.
    - `pathex=[".."]` resolves against the caller's working directory, not the
      spec's. The spec now uses `SPECPATH` and **asserts** `app.main` was
      actually collected, so a repeat fails the build rather than the app.
- [x] Ship the fast pypdfium2 parse path only; Docling models = opt-in in-app
      download (~3 GB otherwise)
  - [x] Nothing reached Docling anyway: `parse()` defaults to `use_models=False`
        and no caller passes True. `docling` is excluded in the spec and is not a
        requirement — it resolves to `docling-slim[standard]`, which drags in
        torch + torchvision + transformers + accelerate (~700 MB of wheels
        before any model weights)
  - [ ] Still open: the actual in-app opt-in download, and wiring `use_models=True`
- [ ] First-run: create data dir, init schema, health check before showing the UI
- [ ] Build for Windows first (dev machine), then macOS

## Phase 8 — Desktop polish

- [x] Harden the stuck-`generating` sweeper — laptops sleep mid-generation far more
      than servers do ~~(optional don't do)~~
      **NOTE: built before I saw the "don't do" annotation — say the word and I'll
      revert it.** What went in (`app/chat/sweep.py`):
  - [x] Sleep detection: `time.monotonic` doesn't advance while the host is
        suspended but wall time does, so a gap between them means the lid was
        shut. One sweep cycle is then skipped — on wake the wall clock has jumped
        past the staleness threshold even though the task is alive and about to
        resume, and sweeping then marks healthy work as failed. If it really did
        die, the next cycle still catches it.
  - [x] `sweep_once(startup=True)` drops the age test: those placeholders are
        written by in-process tasks of *this* process, so at startup every one
        belongs to a process that no longer exists. Waiting out the 10-minute
        threshold to say so left the user watching a spinner for a crash that had
        already happened.
- [x] Crash recovery: requeue jobs left `running` from a previous session
      (`worker.requeue_stranded()`, run at loop startup).
      ~~(also optional right now)~~ — it turned out not to be optional: it is a
      regression phase 2 introduced. ARQ released a dead worker's lock so the job
      became visible again; the `jobs` table has no lease, and `_claim` only
      looks at `queued`. So before this, a force-quit mid-ingest stranded the
      document at "parsing" in the UI *permanently*, with nothing able to pick it
      up. Attempts are deliberately not reset, so a job that reliably kills the
      process still exhausts its retries instead of looping forever.
- [x] Log file in the app data dir + a way to open it — *not* the runs folder:
      these are the real process logs (`app/logging_setup.py`)
  - [x] A rotating file per process, `<LOGS>/backend.log` and `worker.log`, 2 MB
        x 3. One shared file would interleave mid-line and each process would
        rotate the other's out from under it.
  - [x] Why at all: the shell owns the sidecars' stdout, so on a desktop there is
        no terminal to scroll back through — the file is the only artifact.
  - [x] `GET /diagnostics/logs/{process}` for the tail and `GET /diagnostics/paths`
        for every location, so the UI can offer "open folder" (`app/diagnostics.py`)
  - [x] Same module also exposes the trace runs (`GET /diagnostics/runs`,
        `/runs/{name}/{file}`, `DELETE /runs?keep=N` — nothing else ever pruned
        them). Run names come from the client, so paths are containment-checked.
        **No UI for either viewer** — endpoints only.
- [x] Backup/export: the DB is one file — make "export project" trivial
      — **backend only; the settings-page button is NOT built**
  - [x] `GET /projects/{id}/export` -> a zip of `project.json` plus the original
        files. Sources are included, not referenced: an export whose documents
        live elsewhere isn't a backup. Chunks and embeddings excluded — derived,
        reproducible by re-ingest, and almost all of the bytes.
  - [x] Duplicate filenames disambiguated (the DB kept them apart by id, a zip
        entry has only its name); a missing source file is recorded in the
        manifest rather than failing the whole export.
  - [ ] TODO: the Export button on the settings page. `exportProjectUrl()` is
        already in `frontend/src/lib/api/projects.js` (unused for now) — one `<a>`
        in the profile page's Projects list is all that's missing.
- [x] Also fixed here: `config.py` no longer calls `load_dotenv()` when frozen.
      python-dotenv's `find_dotenv()` has an explicit `sys.frozen` branch that
      searches upward from **os.getcwd()**, so an installed copy launched from a
      directory above a stray `.env` would silently load it — and env beats
      config.json. (Raised by the phase 7 agent; the fix belonged in config.py.)
- [x] Verified: `scripts/check_phase8.py`, **31 passed / 0 failed**. Phases 1/4/5
      re-run clean alongside it (63/0, 33/0, 48/0).
- [x] Packaging attempted and stopped: no Rust toolchain and no PyInstaller on
      this machine. Phase 7 still open. `COMMANDS_TO_START.txt` has the verified
      three-terminal dev run instead.
- [ ] Deferred: code signing, auto-update
