"""Verify the SQLite job queue that replaced Redis/ARQ.

    python -m scripts.check_job_queue          (from backend/, venv active)

Written for TODO_desktop.md phase 2. The queue is the part of the port with the
least visible failure mode: a job that is silently never claimed, or claimed
twice, looks exactly like "the upload is taking a while" from the UI. So each
property gets an assertion rather than a manual once-over.

Same conventions as scripts/check_sqlite_port.py: F7OWS_DATA_DIR points at a
throwaway directory, and only the network (`embed`) is stubbed.

The last section is the one that matters most — it enqueues through the real
producer, runs the real `run_worker()` loop as a task, and waits for a real PDF
to come out the other side as chunks. That is the path the app actually takes.
"""
import asyncio
import json
import os
import pathlib
import random
import shutil
import sys
import tempfile
import uuid

SCRATCH = os.path.join(tempfile.gettempdir(), "f7ows_jobq")
shutil.rmtree(SCRATCH, ignore_errors=True)
os.environ["F7OWS_DATA_DIR"] = SCRATCH

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"   {detail}" if detail else ""))


DIM = 1024


def fake_vec(seed):
    r = random.Random(seed)
    v = [r.gauss(0, 1) for _ in range(DIM)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


async def job_row(job_id):
    from app.db import acquire
    async with acquire() as c:
        return await c.fetchrow("SELECT * FROM jobs WHERE id = ?", job_id)


async def main():
    from app import db, producer, worker
    from app.db import acquire

    await db.create_pool()

    # ── 1. enqueue ─────────────────────────────────────────────────────────
    print("\n1. enqueue")
    jid = await producer.enqueue("recalculate_clusters", "abc")
    row = await job_row(jid)
    check("insert returns an id", isinstance(jid, int) and jid > 0, str(jid))
    check("state starts queued", row["state"] == "queued")
    check("func recorded", row["func"] == "recalculate_clusters")
    check("args stored as a json array", json.loads(row["args"]) == ["abc"])
    check("attempts starts at 0", row["attempts"] == 0)

    uid = uuid.uuid4()
    jid2 = await producer.enqueue("ingest_document", uid)
    check("UUID args are stringified", json.loads((await job_row(jid2))["args"]) == [str(uid)])

    # ── 2. claim ───────────────────────────────────────────────────────────
    print("\n2. claim")
    got = await worker._claim()
    check("claims the oldest job first", got["id"] == jid, f"got {got['id']}, wanted {jid}")
    after = await job_row(jid)
    check("claim marks it running", after["state"] == "running", after["state"])
    check("claim increments attempts", after["attempts"] == 1, str(after["attempts"]))

    got2 = await worker._claim()
    check("second claim gets the next job", got2["id"] == jid2)
    check("queue now empty", await worker._claim() is None)

    # a running job is not re-claimable
    async with acquire() as c:
        await c.execute("UPDATE jobs SET state = 'running' WHERE id = ?", jid)
    check("running jobs are not re-claimed", await worker._claim() is None)

    # ── 3. run_after / deferral ────────────────────────────────────────────
    print("\n3. run_after")
    async with acquire() as c:
        await c.execute("DELETE FROM jobs")
    deferred = await producer.enqueue("recalculate_clusters", "later", delay_seconds=30)
    check("deferred job is not yet eligible", await worker._claim() is None)
    async with acquire() as c:
        r = await c.fetchrow("SELECT run_after > strftime('%Y-%m-%dT%H:%M:%fZ','now') AS future "
                             "FROM jobs WHERE id = ?", deferred)
    check("run_after is in the future", r["future"] == 1)
    async with acquire() as c:
        await c.execute("UPDATE jobs SET run_after = strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 seconds') "
                        "WHERE id = ?", deferred)
    check("eligible once run_after passes", (await worker._claim())["id"] == deferred)

    # ── 4. dispatch + outcomes ─────────────────────────────────────────────
    print("\n4. run_job")
    async with acquire() as c:
        await c.execute("DELETE FROM jobs")

    calls = []

    async def ok_job(a, b):
        calls.append((a, b))
        return {"status": "ok"}

    worker.JOBS["_test_ok"] = ok_job
    jid = await producer.enqueue("_test_ok", "x", 2)
    await worker.run_job(await worker._claim())
    check("job function called with its args", calls == [("x", 2)], str(calls))
    check("success marks done", (await job_row(jid))["state"] == "done")

    worker.JOBS["_test_boom"] = None  # placeholder, replaced below
    attempts = {"n": 0}

    async def boom():
        attempts["n"] += 1
        raise RuntimeError("nope")

    worker.JOBS["_test_boom"] = boom
    bid = await producer.enqueue("_test_boom")
    await worker.run_job(await worker._claim())
    r = await job_row(bid)
    check("failure is retried, not errored", r["state"] == "queued", r["state"])
    check("failure records the message", "nope" in (r["error"] or ""), str(r["error"]))
    check("retry is backed off",
          r["run_after"] > r["created_at"], f"{r['run_after']} vs {r['created_at']}")

    # exhaust the retries
    for _ in range(worker._MAX_ATTEMPTS):
        async with acquire() as c:
            await c.execute("UPDATE jobs SET run_after = "
                            "strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 seconds') WHERE id = ?", bid)
        job = await worker._claim()
        if job is None:
            break
        await worker.run_job(job)
    r = await job_row(bid)
    check("gives up after _MAX_ATTEMPTS", r["state"] == "error", r["state"])
    check("attempt count is capped", attempts["n"] == worker._MAX_ATTEMPTS,
          f"ran {attempts['n']}, max {worker._MAX_ATTEMPTS}")

    uid = await producer.enqueue("no_such_function")
    await worker.run_job(await worker._claim())
    r = await job_row(uid)
    check("unknown function errors, does not crash", r["state"] == "error", r["state"])
    check("unknown function says so", "unknown job function" in (r["error"] or ""))

    # ── 5. finalize_project re-enqueue (the _defer_by replacement) ─────────
    print("\n5. finalize_project deferral")
    async with acquire() as c:
        await c.execute("DELETE FROM jobs")
        u = uuid.uuid4()
        await c.execute("INSERT INTO users (id, email, hashed_password) VALUES (?, ?, ?)",
                        u, "q@b.c", "x")
        pid = uuid.uuid4()
        await c.execute("INSERT INTO projects (id, user_id, name) VALUES (?, ?, ?)",
                        pid, u, "P")
        did = uuid.uuid4()
        await c.execute(
            "INSERT INTO documents (id, project_id, name, storage_key, status) "
            "VALUES (?, ?, ?, ?, ?)",
            did, pid, "d.pdf", f"documents/{pid}/{did}/d.pdf",
            json.dumps({"state": "parsing"}))

    res = await worker.finalize_project(str(pid), [str(did)])
    check("waits while a document is mid-ingest", res["status"] == "waiting", str(res))
    async with acquire() as c:
        q = await c.fetch("SELECT func, args, run_after > strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                          "AS future FROM jobs")
    check("re-enqueued itself once", len(q) == 1 and q[0]["func"] == "finalize_project",
          str([dict(x) for x in q]))
    check("re-enqueue is deferred", q[0]["future"] == 1)
    check("attempts_left was decremented",
          json.loads(q[0]["args"])[2] == worker._FINALIZE_MAX_ATTEMPTS - 1,
          str(json.loads(q[0]["args"])))

    async with acquire() as c:
        await c.execute("DELETE FROM jobs")
        await c.execute("UPDATE documents SET status = ? WHERE id = ?",
                        json.dumps({"state": "ready"}), did)
    res = await worker.finalize_project(str(pid), [str(did)])
    check("finalizes once documents are terminal", res["status"] == "finalized", str(res))
    async with acquire() as c:
        q = [r["func"] for r in await c.fetch("SELECT func FROM jobs ORDER BY id")]
    check("enqueues both recomputes",
          q == ["recalculate_projections", "recalculate_clusters"], str(q))

    # the ceiling: attempts_left exhausted means recompute anyway, not wait forever
    async with acquire() as c:
        await c.execute("DELETE FROM jobs")
        await c.execute("UPDATE documents SET status = ? WHERE id = ?",
                        json.dumps({"state": "parsing"}), did)
    res = await worker.finalize_project(str(pid), [str(did)], attempts_left=0)
    check("gives up waiting and recomputes anyway", res["status"] == "finalized", str(res))

    # ── 5b. crash recovery ─────────────────────────────────────────────────
    print("\n5b. stranded jobs (worker killed mid-run)")
    async with acquire() as c:
        await c.execute("DELETE FROM jobs")
    live = await producer.enqueue("recalculate_clusters", "still-queued")
    stranded = await producer.enqueue("recalculate_clusters", "was-running")
    claimed = await worker._claim()          # simulate: claimed, then the process dies
    check("a claimed job is left running", claimed["id"] == live)
    async with acquire() as c:
        await c.execute("UPDATE jobs SET state = 'running' WHERE id = ?", stranded)
        before = await c.fetchval("SELECT count(*) FROM jobs WHERE state = 'running'")
    check("two jobs are stranded", before == 2, str(before))

    # nothing would ever pick these up again
    check("stranded jobs are invisible to _claim", await worker._claim() is None)

    n = await worker.requeue_stranded()
    check("requeue_stranded returns them to the queue", n == 2, str(n))
    async with acquire() as c:
        states = {r["state"]: r["n"] for r in await c.fetch(
            "SELECT state, count(*) AS n FROM jobs GROUP BY state")}
        attempts = await c.fetchval("SELECT attempts FROM jobs WHERE id = ?", live)
    check("they are queued again", states == {"queued": 2}, str(states))
    check("attempts are preserved, not reset", attempts == 1, str(attempts))
    check("and claimable once more", (await worker._claim()) is not None)
    # That claim left one job running again, so this sweeps exactly it — which is
    # also the check that requeue only ever touches `running` rows.
    check("requeue only touches running rows", await worker.requeue_stranded() == 1)
    check("requeue is a no-op on a clean queue", await worker.requeue_stranded() == 0)

    # ── 6. end to end through the real loop ────────────────────────────────
    print("\n6. end to end: producer -> run_worker() -> chunks")
    from app.library import storage
    async with acquire() as c:
        await c.execute("DELETE FROM jobs")
        await c.execute("UPDATE documents SET status = ? WHERE id = ?",
                        json.dumps({"state": "uploaded"}), did)

    pdf = pathlib.Path("testsuite/pdfs_to_upload/02_tokens.pdf")
    check("test PDF present", pdf.exists(), str(pdf))
    await storage.upload_bytes(f"documents/{pid}/{did}/d.pdf", pdf.read_bytes(),
                               "application/pdf")

    seed = iter(range(100_000))

    async def fake_embed(texts):
        return [fake_vec(next(seed)) for _ in texts]

    worker.embed = fake_embed

    # Enqueue exactly the way the upload endpoint does.
    await producer.enqueue_ingest(did)
    await producer.enqueue_finalize(pid, [did])

    loop_task = asyncio.create_task(worker.run_worker())
    try:
        for _ in range(600):                      # up to 60s
            await asyncio.sleep(0.1)
            async with acquire() as c:
                st = json.loads(await c.fetchval(
                    "SELECT status FROM documents WHERE id = ?", did))
                pending = await c.fetchval(
                    "SELECT count(*) FROM jobs WHERE state IN ('queued','running')")
            if st.get("state") in ("ready", "error") and pending == 0:
                break
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    check("document reached ready", st.get("state") == "ready", str(st))
    async with acquire() as c:
        n_chunks = await c.fetchval("SELECT count(*) FROM chunks WHERE document_id = ?", did)
        states = {r["state"]: r["n"] for r in await c.fetch(
            "SELECT state, count(*) AS n FROM jobs GROUP BY state")}
        funcs = [r["func"] for r in await c.fetch(
            "SELECT func FROM jobs WHERE state = 'done' ORDER BY id")]
        n_coords = await c.fetchval(
            "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.project_id = ? AND c.umap_2d IS NOT NULL", pid)
    check("chunks were written by the worker", n_chunks > 0, f"{n_chunks} chunks")
    check("every job finished", states.get("queued", 0) == 0 and states.get("running", 0) == 0,
          str(states))
    check("no job errored", states.get("error", 0) == 0, str(states))
    check("the whole pipeline ran",
          funcs == ["ingest_document", "finalize_project",
                    "recalculate_projections", "recalculate_clusters"], str(funcs))
    check("projections landed", n_coords == n_chunks, f"{n_coords}/{n_chunks}")

    await db.close_pool()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
