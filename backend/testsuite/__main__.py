"""Testsuite CLI.

    python -m testsuite ingest     # upload every PDF in pdfs_to_upload/ and ingest
    python -m testsuite chat       # terminal chat REPL against the test project
    python -m testsuite clean      # wipe the test project's data (add --runs to clear runs/)

`ingest` and `chat` auto-start uvicorn + the worker for you. Nothing external
needs to be running: the database is a file and the queue is a table in it.
Each real call drops a trace into runs/.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from uuid import UUID

from testsuite.client import Api
from testsuite.config import (PDF_DIR, RUNS_DIR, TEST_EMAIL, TEST_PROJECT_NAME)


def _wait_for_finalize(project_id: str, *, timeout: float = 180.0, interval: float = 2.0) -> None:
    """Wait for the project-wide recompute (projections + clusters) that runs after
    ingest in finalize_project. No HTTP surface for this yet, so the test harness
    polls the DB directly — otherwise we'd tear the worker down before it finishes
    and never get the project_<id>/ trace artifacts."""
    import asyncio
    import time

    from app.db import acquire, close_pool, create_pool

    async def _poll() -> None:
        await create_pool()
        try:
            deadline = time.time() + timeout
            while True:
                async with acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        SELECT
                          (SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id
                             WHERE d.project_id = ?) AS total,
                          (SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id
                             WHERE d.project_id = ? AND c.umap_2d IS NOT NULL) AS projected,
                          (SELECT COUNT(*) FROM clusters WHERE project_id = ?) AS clusters
                        """,
                        # Three placeholders, one value: positional parameters are
                        # matched by position, so a repeated $1 has to be repeated.
                        UUID(project_id), UUID(project_id), UUID(project_id),
                    )
                if row["total"] and row["projected"] >= row["total"]:
                    print(f"  projections done ({row['projected']}/{row['total']} chunks), "
                          f"clusters={row['clusters']}")
                    return
                if time.time() > deadline:
                    print(f"  (warning: finalize not confirmed in {timeout}s — "
                          f"projected={row['projected']}/{row['total']}, clusters={row['clusters']})")
                    return
                await asyncio.sleep(interval)
        finally:
            await close_pool()

    asyncio.run(_poll())


def _setup(api: Api):
    user = api.ensure_user()
    project = api.ensure_project()
    print(f"[setup] user={user['email']}  project={project['name']} ({project['id']})")
    return project


def cmd_ingest(_args) -> int:
    from testsuite.procs import managed_backend

    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs in {PDF_DIR} — drop some in and re-run.")
        return 1
    print(f"[ingest] {len(pdfs)} PDF(s): " + ", ".join(p.name for p in pdfs))

    with managed_backend():
        api = Api()
        try:
            project = _setup(api)
            result = api.upload_pdfs(project["id"], pdfs)
            for err in result.get("errors", []):
                print(f"  ! rejected {err['filename']}: {err['error']}")
            ids = {d["id"] for d in result.get("uploaded", [])}
            print(f"[ingest] {len(ids)} accepted; waiting for the worker to finish…")

            def _tick(docs):
                line = "  ".join(f"{d['name'][:20]}={(d['status'] or {}).get('state')}" for d in docs)
                print(f"\r  {line}", end="", flush=True)

            docs = api.wait_until_ready(project["id"], on_tick=_tick)
            print("\n[ingest] documents ready; waiting for projections + clusters…")
            _wait_for_finalize(project["id"])
            print("[ingest] done:")
            for d in docs:
                st = d["status"] or {}
                print(f"  - {d['name']}: {st.get('state')} ({st.get('chunks', '?')} chunks)")
            print(f"\nTraces in {RUNS_DIR}")
        finally:
            api.close()
    return 0


def cmd_chat(_args) -> int:
    from testsuite.procs import managed_backend
    from testsuite.chat_repl import run_repl

    with managed_backend(quiet_worker=True):
        api = Api()
        try:
            project = _setup(api)
            docs = api.list_documents(project["id"])
            ready = [d for d in docs if (d["status"] or {}).get("state") == "ready"]
            if not ready:
                print("  (warning: no ready documents — run `python -m testsuite ingest` first)")
            run_repl(api, project["id"])
        finally:
            api.close()
    return 0


def cmd_clean(args) -> int:
    import asyncio

    from app.db import acquire, close_pool, create_pool

    async def _wipe() -> None:
        await create_pool()
        try:
            async with acquire() as conn:
                uid = await conn.fetchval("SELECT id FROM users WHERE email = ?", TEST_EMAIL)
                if uid is None:
                    print(f"[clean] no test user ({TEST_EMAIL}) — nothing to wipe")
                else:
                    # Deleting projects cascades to documents/chunks/messages/clusters/artifacts.
                    cursor = await conn.execute(
                        "DELETE FROM projects WHERE user_id = ? AND name = ?", uid, TEST_PROJECT_NAME
                    )
                    print(f"[clean] deleted {cursor.rowcount} "
                          f"(test project '{TEST_PROJECT_NAME}')")
        finally:
            await close_pool()
            # Flush the queue too: a previous interrupted run leaves jobs behind
            # that the next worker drains against now-deleted documents (noisy
            # "document not found" failures) and that can starve fresh jobs.
            # Same connection as the wipe above — the queue is a table now.
            async with acquire() as conn:
                cursor = await conn.execute("DELETE FROM jobs")
                print(f"[clean] flushed {cursor.rowcount} queued job(s)")

    asyncio.run(_wipe())
    if args.runs and RUNS_DIR.exists():
        shutil.rmtree(RUNS_DIR)
        print(f"[clean] removed {RUNS_DIR}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="testsuite", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="upload + ingest every PDF in pdfs_to_upload/")
    sub.add_parser("chat", help="terminal chat REPL against the test project")
    p_clean = sub.add_parser("clean", help="wipe the test project's data")
    p_clean.add_argument("--runs", action="store_true", help="also delete the runs/ trace folder")

    args = parser.parse_args()
    return {"ingest": cmd_ingest, "chat": cmd_chat, "clean": cmd_clean}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
