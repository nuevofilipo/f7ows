"""Verify desktop polish (TODO_desktop.md phase 8).

    python scripts/check_phase8.py        (from backend/, venv active)

Sweeper behaviour on wake, file logging, the diagnostics endpoints, and a real
project export round-tripped through zipfile.
"""
import asyncio
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
import zipfile

SCRATCH = os.path.join(tempfile.gettempdir(), "f7ows_verify_p8")
shutil.rmtree(SCRATCH, ignore_errors=True)
os.environ["F7OWS_DATA_DIR"] = SCRATCH
os.environ["TRACE"] = "0"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "ok  " if cond else "FAIL"
    print(f"  {mark} {name}" + (f"   {detail}" if detail else ""))


async def db_checks():
    from app.chat import sweep
    from app.db import acquire, close_pool, create_pool
    from app.library import storage
    from app.local_user import ensure_local_user

    print("\n1. sweeper")
    check("a normal interval is not mistaken for sleep", not sweep._slept(121.0, 120.0))
    check("a lid closed for an hour is detected", sweep._slept(3600.0, 5.0))
    check("the threshold is one interval of slack",
          not sweep._slept(120.0 + sweep.SWEEP_INTERVAL_SECONDS, 120.0))

    await create_pool()
    try:
        uid = await ensure_local_user()
        pid, cid = str(uuid.uuid4()), str(uuid.uuid4())
        async with acquire() as c:
            await c.execute("INSERT INTO projects (id, user_id, name) VALUES (?, ?, ?)",
                            pid, uid, "Export Test")
            # A message left mid-generation by a process that no longer exists.
            await c.execute(
                "INSERT INTO messages (id, project_id, chat_id, role, content, metadata, path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                str(uuid.uuid4()), pid, cid, "assistant", "",
                json.dumps({"state": "generating"}), "0001",
            )

        n = await sweep.sweep_once()
        check("a fresh 'generating' survives an ordinary sweep", n == 0)
        n = await sweep.sweep_once(startup=True)
        check("but a startup sweep clears it without waiting out the threshold", n == 1)
        async with acquire() as c:
            state = await c.fetchval(
                "SELECT json_extract(metadata, '$.state') FROM messages WHERE chat_id = ?", cid)
        check("and it is marked error, not left pending", state == "error")

        # Two documents deliberately share a filename.
        for i in range(2):
            did = str(uuid.uuid4())
            key = f"documents/{pid}/{did}/notes.pdf"
            await storage.upload_bytes(key, f"%PDF-fake-{i}".encode())
            async with acquire() as c:
                await c.execute(
                    "INSERT INTO documents (id, project_id, name, storage_key, content_type, "
                    "size_bytes) VALUES (?, ?, ?, ?, ?, ?)",
                    did, pid, "notes.pdf", key, "application/pdf", 12,
                )
        # A third whose file is gone from disk.
        did = str(uuid.uuid4())
        async with acquire() as c:
            await c.execute(
                "INSERT INTO documents (id, project_id, name, storage_key, content_type, "
                "size_bytes) VALUES (?, ?, ?, ?, ?, ?)",
                did, pid, "missing.pdf", f"documents/{pid}/{did}/missing.pdf",
                "application/pdf", 12,
            )
    finally:
        await close_pool()
    return pid


def http_checks(pid):
    from fastapi.testclient import TestClient

    from app import logging_setup, paths
    from app.main import app

    print("\n2. logging")
    with TestClient(app) as client:
        path = logging_setup.log_path("backend")
        logging.getLogger("check_phase8").info("a canary line")
        check("the log file is created in the log dir", path.is_file(), str(path))
        check("it is under <LOGS>, not next to the code", path.parent == paths.log_dir())
        check("tail() returns what was written", "canary line" in logging_setup.tail("backend"))
        before = len(logging.getLogger().handlers)
        logging_setup.setup("backend")
        check("setup() is idempotent — no duplicated handlers",
              len(logging.getLogger().handlers) == before)

        print("\n3. diagnostics API")
        r = client.get("/diagnostics/paths")
        check("GET /diagnostics/paths -> 200", r.status_code == 200)
        check("reports the database and log locations",
              r.json()["database"].endswith("db.sqlite3") and "backend" in r.json()["log_files"])

        r = client.get("/diagnostics/logs/backend?lines=50")
        check("GET /diagnostics/logs/backend -> 200", r.status_code == 200)
        check("returns real log text", "canary line" in r.json()["text"])
        check("an unknown process is a 404", client.get("/diagnostics/logs/nope").status_code == 404)

        # Write a trace run to read back.
        run = paths.runs_dir() / "run1_chat_probe"
        run.mkdir(parents=True, exist_ok=True)
        (run / "prompt.md").write_text("# Prompt sent to the LLM", encoding="utf-8")

        r = client.get("/diagnostics/runs")
        check("GET /diagnostics/runs lists the run",
              any(x["name"] == "run1_chat_probe" for x in r.json()["runs"]))
        check("and lists its artifacts",
              "prompt.md" in r.json()["runs"][0]["files"])
        r = client.get("/diagnostics/runs/run1_chat_probe/prompt.md")
        check("an artifact reads back", "Prompt sent to the LLM" in r.json()["text"])
        r = client.get("/diagnostics/runs/..%2f..%2f..%2fconfig.json/x")
        check("traversal out of the runs dir is refused", r.status_code in (400, 404))

        r = client.delete("/diagnostics/runs?keep=0")
        check("pruning removes runs", r.status_code == 200 and r.json()["removed"] >= 1)

        print("\n4. project export")
        r = client.get(f"/projects/{pid}/export")
        check("GET /projects/{id}/export -> 200", r.status_code == 200)
        check("is served as a zip attachment",
              r.headers["content-type"] == "application/zip"
              and "attachment" in r.headers["content-disposition"])
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = zf.namelist()
        check("contains the manifest", "project.json" in names)
        manifest = json.loads(zf.read("project.json"))
        check("manifest names the format", manifest["format"] == "f7ows-project-export/1")
        check("manifest carries the project, documents and chats",
              manifest["project"]["name"] == "Export Test"
              and len(manifest["documents"]) == 3 and len(manifest["chats"]) == 1)
        docs = [n for n in names if n.startswith("documents/")]
        check("the source files are inside, not referenced", len(docs) == 2, str(docs))
        check("a shared filename is disambiguated, not overwritten", len(set(docs)) == 2)
        check("file content survives", zf.read(docs[0]).startswith(b"%PDF-fake"))
        check("a missing source file is recorded, not fatal",
              manifest.get("missing_files") == ["missing.pdf"])
        check("derived data is excluded", not any("chunk" in n or "embed" in n for n in names))
        r = client.get(f"/projects/{uuid.uuid4()}/export")
        check("exporting an unknown project is a 404", r.status_code == 404)


pid = asyncio.run(db_checks())
http_checks(pid)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"  - {f}")
sys.exit(1 if FAIL else 0)
