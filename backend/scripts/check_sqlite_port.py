"""Verify the SQLite port: drive every ported SQL statement against a scratch DB.

    python -m scripts.check_sqlite_port          (from backend/, venv active)

Written for the Postgres -> SQLite port (TODO_desktop.md phase 1). Roughly forty
statements moved from asyncpg/ltree/jsonb/pgvector to aiosqlite/GLOB/json1/
sqlite-vec, and most of them sit behind a route or a worker job that no unit test
covers. This exercises each one against a throwaway database and asserts on what
came back, so a regression shows up as a named failure rather than a 500 on a
rarely-hit endpoint.

It points F7OWS_DATA_DIR at a temp directory, so it can never touch devdata or an
installed copy, and it starts from an empty file every run (which also
re-exercises migrate()).

Only the network is stubbed: `embed` is replaced with deterministic unit vectors,
because a real embedding costs money and proves nothing about SQL. Everything
else is the real code path -- real routers, real worker jobs, a real PDF through
the real parser, chunker and local storage.
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

SCRATCH = os.path.join(tempfile.gettempdir(), "f7ows_verify")
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


async def main():
    from app import db
    from app.db import acquire

    conn = await db.create_pool()

    # ── 1. auth ────────────────────────────────────────────────────────────
    print("\n1. auth")
    from app.auth import auth

    async with acquire() as c:
        row = await c.fetchrow(
            """
            INSERT INTO users (id, email, hashed_password)
            VALUES (?, ?, ?)
            ON CONFLICT (email) DO NOTHING
            RETURNING id, email, settings, openrouter_key, created_at
            """,
            uuid.uuid4(), "a@b.c", auth.hash_password("password123"),
        )
    check("register inserts and RETURNs", row is not None)
    user = auth._user_dict(row)
    uid = user["id"]
    check("id comes back as str", isinstance(uid, str), type(uid).__name__)
    check("settings parsed to dict", user["settings"] == {})
    # Phase 4 moved the BYOK key out of the users row into the OS keychain, so
    # _user_dict no longer surfaces it. The `in` trap this guarded is a property
    # of db.Row, not of that field — assert it directly and keep the coverage.
    check("'x' in row tests column NAMES, not values (the sqlite3.Row trap)",
          "email" in row and "hashed_password" not in row and "a@b.c" not in row)

    async with acquire() as c:
        dup = await c.fetchrow(
            """
            INSERT INTO users (id, email, hashed_password)
            VALUES (?, ?, ?)
            ON CONFLICT (email) DO NOTHING
            RETURNING id
            """,
            uuid.uuid4(), "a@b.c", "x",
        )
    check("duplicate email returns no row", dup is None)

    async with acquire() as c:
        me = await c.fetchrow(
            "SELECT id, email, settings, openrouter_key, created_at FROM users WHERE id = ?",
            uuid.UUID(uid),
        )
    check("lookup by UUID object binds", me is not None)

    # ── 2. projects ────────────────────────────────────────────────────────
    print("\n2. projects")
    from app.db import NOW

    async with acquire() as c:
        proj = await c.fetchrow(
            """
            INSERT INTO projects (id, user_id, name, description)
            VALUES (?, ?, ?, ?)
            RETURNING id, name, description, created_at, updated_at
            """,
            uuid.uuid4(), uid, "P", None,
        )
    pid = proj["id"]
    check("project insert", proj["name"] == "P")

    fields, args = ["name = ?"], ["renamed"]
    fields.append(f"updated_at = {NOW}")
    args.extend([pid, uid])
    async with acquire() as c:
        upd = await c.fetchrow(
            f"""
            UPDATE projects SET {", ".join(fields)}
            WHERE id = ? AND user_id = ?
            RETURNING id, name, description, created_at, updated_at
            """,
            *args,
        )
    check("partial UPDATE + NOW()", upd is not None and upd["name"] == "renamed")
    check("updated_at is ISO-8601", str(upd["updated_at"]).endswith("Z"), str(upd["updated_at"]))

    # ── 3. documents ───────────────────────────────────────────────────────
    print("\n3. documents")
    did = uuid.uuid4()
    async with acquire() as c:
        doc = await c.fetchrow(
            """
            INSERT INTO documents (id, project_id, name, storage_key, content_type, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id, name, storage_key, content_type, size_bytes, status, created_at
            """,
            did, pid, "test.pdf", f"documents/{pid}/{did}/test.pdf", "application/pdf", 10,
        )
    from app.library.documents import _document_dict
    check("document insert + default status", _document_dict(doc)["status"] == {"state": "uploaded"})

    async with acquire() as c:
        owner = await c.fetchval(
            "SELECT 1 FROM projects WHERE id = ? AND user_id = ?", pid, uid)
    check("ownership check", owner == 1)

    # ── 4. preferences ─────────────────────────────────────────────────────
    print("\n4. preferences")
    async with acquire() as c:
        await c.execute("UPDATE users SET settings = ? WHERE id = ?",
                        json.dumps({"chat_model": "x"}), uid)
        got = await c.fetchval("SELECT settings FROM users WHERE id = ?", uid)
    check("settings round-trip", json.loads(got) == {"chat_model": "x"})

    async with acquire() as c:
        await c.execute("UPDATE users SET openrouter_key = ? WHERE id = ?", "enc", uid)
        k1 = await c.fetchval("SELECT openrouter_key FROM users WHERE id = ?", uid)
        await c.execute("UPDATE users SET openrouter_key = NULL WHERE id = ?", uid)
        k2 = await c.fetchval("SELECT openrouter_key FROM users WHERE id = ?", uid)
    check("key set then cleared", k1 == "enc" and k2 is None)

    # ── 5. worker ingest (chunks + vectors) ────────────────────────────────
    print("\n5. worker: ingest_document")
    from app import worker
    from app.library import storage

    # A real PDF through the real local storage and the real parse+chunk path.
    # Only the network embed call is stubbed: it costs money and proves nothing
    # about the SQL. Everything the port touched is exercised for real.
    pdf = pathlib.Path("testsuite/pdfs_to_upload/02_tokens.pdf")
    check("test PDF present", pdf.exists(), str(pdf))
    await storage.upload_bytes(f"documents/{pid}/{did}/test.pdf", pdf.read_bytes(),
                               "application/pdf")
    async with acquire() as c:
        await c.execute("UPDATE documents SET storage_key = ? WHERE id = ?",
                        f"documents/{pid}/{did}/test.pdf", did)

    seed = iter(range(100_000))

    async def fake_embed(texts):
        return [fake_vec(next(seed)) for _ in texts]

    worker.embed = fake_embed

    result = await worker.ingest_document(str(did))
    check("ingest_document ok", result["status"] == "ok" and result["chunks"] > 0, str(result))

    async with acquire() as c:
        n_chunks = await c.fetchval("SELECT count(*) FROM chunks WHERE document_id = ?", did)
        n_vecs = await c.fetchval(
            "SELECT count(*) FROM chunk_vectors v JOIN chunks c ON c.seq = v.chunk_seq "
            "WHERE c.document_id = ?", did)
        n_fts = await c.fetchval(
            "SELECT count(*) FROM chunks_fts f JOIN chunks c ON c.seq = f.rowid "
            "WHERE c.document_id = ?", did)
        st = await c.fetchval("SELECT status FROM documents WHERE id = ?", did)
    check("chunk/vector counts line up", n_chunks == n_vecs == result["chunks"],
          f"chunks={n_chunks} vectors={n_vecs}")
    check("FTS index populated by trigger", n_fts == n_chunks, f"fts={n_fts}")
    check("status -> ready", json.loads(st)["state"] == "ready", st)

    # re-ingest must not duplicate, and must not orphan vectors
    await worker.ingest_document(str(did))
    async with acquire() as c:
        n2 = await c.fetchval("SELECT count(*) FROM chunks WHERE document_id = ?", did)
        orphans = await c.fetchval(
            "SELECT count(*) FROM chunk_vectors v "
            "LEFT JOIN chunks c ON c.seq = v.chunk_seq WHERE c.seq IS NULL")
    check("re-ingest is idempotent", n2 == n_chunks, f"{n2} vs {n_chunks}")
    check("delete trigger reaped old vectors", orphans == 0, f"orphans={orphans}")

    # ── 6. retrieval ───────────────────────────────────────────────────────
    print("\n6. retrieval")
    from app.retrieval import retrieval_router as rr
    rr.embed = fake_embed

    spec = await rr._specific(pid, "what is a section", 3)
    check("_specific returns k rows", len(spec.chunks) == 3, str(len(spec.chunks)))
    check("_specific distances ascending",
          all(spec.chunks[i]["distance"] <= spec.chunks[i + 1]["distance"]
              for i in range(len(spec.chunks) - 1)))

    stuff = await rr._stuff(pid, 100_000)
    check("_stuff returns all chunks", len(stuff.chunks) == n_chunks, str(len(stuff.chunks)))

    scoped = await rr._stuff(pid, 100_000, [did])
    check("_stuff scoped by document", len(scoped.chunks) == n_chunks)
    scoped_none = await rr._stuff(pid, 100_000, [uuid.uuid4()])
    check("_stuff with a foreign document id -> empty", len(scoped_none.chunks) == 0)

    async with acquire() as c:
        paths = [r["path"] for r in await c.fetch(
            "SELECT DISTINCT path FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.project_id = ? ORDER BY path", pid)]
    prefix = paths[0].split(".")[0]
    sql, params = db.descendants_of("c.path", [prefix])
    async with acquire() as c:
        n_desc = await c.fetchval(
            f"SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
            f"WHERE d.project_id = ? AND {sql}", pid, *params)
    check("descendants_of matches whole tree", n_desc == n_chunks, f"{n_desc}/{n_chunks}")

    sql2, params2 = db.descendants_of("c.path", ["no_such_branch"])
    async with acquire() as c:
        n_none = await c.fetchval(
            f"SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
            f"WHERE d.project_id = ? AND {sql2}", pid, *params2)
    check("descendants_of on a missing branch -> 0", n_none == 0)

    # the LIKE-vs-GLOB trap: '_' must not act as a wildcard
    async with acquire() as c:
        await c.execute(
            "INSERT INTO chunks (id, document_id, chunk_index, content_full, path) "
            "VALUES (?, ?, ?, ?, ?)", uuid.uuid4(), did, 9001, "x", "a_b.leaf")
        await c.execute(
            "INSERT INTO chunks (id, document_id, chunk_index, content_full, path) "
            "VALUES (?, ?, ?, ?, ?)", uuid.uuid4(), did, 9002, "x", "axb.leaf")
        sql3, params3 = db.descendants_of("path", ["a_b"])
        hits = await c.fetch(f"SELECT path FROM chunks WHERE {sql3}", *params3)
    check("underscore is literal, not a LIKE wildcard",
          [h["path"] for h in hits] == ["a_b.leaf"], str([h["path"] for h in hits]))
    async with acquire() as c:
        await c.execute("DELETE FROM chunks WHERE chunk_index IN (9001, 9002)")

    # ── 7. chat tree ───────────────────────────────────────────────────────
    print("\n7. chat")
    from app.chat import chat as chatmod

    cid = uuid.uuid4()
    async with acquire() as c:
        roots = []
        for i in range(12):
            p = await chatmod._next_child_path(c, cid, None)
            roots.append(p)
            await chatmod._insert_message(c, pid, cid, p, "user", f"m{i}", {"i": i})
    check("root labels zero-padded", roots[0] == "0001" and roots[11] == "0012", str(roots[:3]))

    async with acquire() as c:
        ordered = [r["path"] for r in await c.fetch(
            "SELECT path FROM messages WHERE chat_id = ? ORDER BY path", cid)]
    check("ORDER BY path == insertion order past 10 siblings", ordered == roots,
          f"{ordered[:4]}...")

    async with acquire() as c:
        kids = []
        for i in range(3):
            p = await chatmod._next_child_path(c, cid, "0002")
            kids.append(p)
            await chatmod._insert_message(c, pid, cid, p, "assistant", f"k{i}", {})
        grandchild = await chatmod._next_child_path(c, cid, "0002.0001")
    check("child labels", kids == ["0002.0001", "0002.0002", "0002.0003"], str(kids))
    check("child count ignores grandchildren", grandchild == "0002.0001.0001", grandchild)

    async with acquire() as c:
        branch, bargs = db.ancestors_of("path", "0002.0002")
        hist = [r["path"] for r in await c.fetch(
            f"SELECT path FROM messages WHERE chat_id = ? AND {branch} ORDER BY path",
            cid, *bargs)]
    check("ancestors_of gives the branch", hist == ["0002", "0002.0002"], str(hist))

    async with acquire() as c:
        chats = await c.fetch(
            """
            SELECT chat_id, COUNT(*) AS message_count, MAX(created_at) AS last_activity
            FROM messages WHERE project_id = ?
            GROUP BY chat_id ORDER BY last_activity DESC
            """, pid)
    check("chat listing aggregates", chats[0]["message_count"] == 15,
          str(chats[0]["message_count"]))

    parsed = [did]
    async with acquire() as c:
        rows = await c.fetch(
            f"SELECT id FROM documents WHERE project_id = ? "
            f"AND id IN ({db.placeholders(parsed)})", pid, *parsed)
    check("source_ids IN-list", len(rows) == 1)

    # ── 8. deepsummary / sweep ─────────────────────────────────────────────
    print("\n8. background message state")
    async with acquire() as c:
        stale = await chatmod._insert_message(
            c, pid, cid, "0099", "assistant", "", {"state": "generating", "level": 1})
        await c.execute(
            "UPDATE messages SET content = ?, metadata = ? WHERE id = ?",
            "done", json.dumps({"state": "ready"}), stale["id"])
        meta = await c.fetchval("SELECT metadata FROM messages WHERE id = ?", stale["id"])
    check("deepsummary result write", json.loads(meta)["state"] == "ready")

    async with acquire() as c:
        gen = await chatmod._insert_message(
            c, pid, cid, "0098", "assistant", "", {"state": "generating"})
        # backdate it past the staleness threshold
        await c.execute(
            "UPDATE messages SET created_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now','-3600 seconds') WHERE id = ?", gen["id"])
    from app.chat.sweep import sweep_once
    swept = await sweep_once()
    async with acquire() as c:
        after = json.loads(await c.fetchval(
            "SELECT metadata FROM messages WHERE id = ?", gen["id"]))
    check("sweep marks stale generating rows", swept == 1, f"rowcount={swept}")
    check("sweep json_patch preserves other keys", after["state"] == "error" and "error" in after,
          str(after))
    check("sweep leaves fresh rows alone", await sweep_once() == 0)

    # ── 9. study ───────────────────────────────────────────────────────────
    print("\n9. study")
    from app.chat.study import ACTIVELY_STUDIED, PASSIVELY_STUDIED, clear_studied, mark_studied

    async with acquire() as c:
        ids = [r["id"] for r in await c.fetch(
            "SELECT c.id FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.project_id = ? ORDER BY c.id LIMIT 3", pid)]
    n = await mark_studied(pid, ids, PASSIVELY_STUDIED)
    check("mark_studied passive", n == 3, f"n={n}")
    n = await mark_studied(pid, ids, ACTIVELY_STUDIED)
    check("upgrade passive -> active", n == 3, f"n={n}")
    n = await mark_studied(pid, ids, PASSIVELY_STUDIED)
    check("monotonic: active is not downgraded", n == 0, f"n={n}")
    n = await mark_studied(uuid.uuid4(), ids, ACTIVELY_STUDIED)
    check("scoped to the project", n == 0, f"n={n}")
    n = await clear_studied(pid)
    check("clear_studied resets", n == 3, f"n={n}")
    check("clear_studied is a no-op second time", await clear_studied(pid) == 0)

    # ── 10. projections api ────────────────────────────────────────────────
    print("\n10. projection api")
    from app.projections import api as papi
    papi.embed = fake_embed

    proj_rows = await worker.recalculate_projections(str(pid))
    check("recalculate_projections", proj_rows["chunks"] == n_chunks, str(proj_rows))

    res = await papi.get_projection(uuid.UUID(pid), {"id": uid})
    check("get_projection ready", res["ready"] is True)
    check("coords parsed to dicts",
          isinstance(res["chunks"][0]["coords"]["umap_2d"], dict),
          str(res["chunks"][0]["coords"])[:80])

    one = res["chunks"][0]["id"]
    content = await papi.get_chunk_content(uuid.UUID(pid), uuid.UUID(one), {"id": uid})
    check("chunk content fetch", len(content["content_full"]) > 0)

    hit = await papi.search_projection(uuid.UUID(pid), q="section body", method="cosine", k=3,
                                       current_user={"id": uid})
    check("cosine search returns k", len(hit["matches"]) == 3)
    check("cosine scores in [-1, 1]", all(-1.01 <= m["score"] <= 1.01 for m in hit["matches"]),
          str(hit["matches"][0]))
    check("cosine scores descending",
          [m["score"] for m in hit["matches"]] == sorted(
              (m["score"] for m in hit["matches"]), reverse=True))

    # Take the search term from the document itself — the fixture is a real PDF,
    # so hardcoding a word would just be testing the fixture.
    import collections
    async with acquire() as c:
        corpus = " ".join(r["content_full"] for r in await c.fetch(
            "SELECT c.content_full FROM chunks c JOIN documents d ON d.id = c.document_id "
            "WHERE d.project_id = ?", pid))
    common = collections.Counter(
        w for w in papi._FTS_TOKEN.findall(corpus.lower()) if len(w) > 4).most_common(1)
    term = common[0][0]
    bm = await papi.search_projection(uuid.UUID(pid), q=term, method="bm25", k=3,
                                      current_user={"id": uid})
    check("bm25 search returns matches", len(bm["matches"]) > 0,
          f"term={term!r} hits={len(bm['matches'])}")
    check("bm25 scores descending",
          [m["score"] for m in bm["matches"]] == sorted(
              (m["score"] for m in bm["matches"]), reverse=True))
    nonsense = await papi.search_projection(uuid.UUID(pid), q="zzzqqq", method="bm25", k=3,
                                            current_user={"id": uid})
    check("bm25 with no match -> empty", nonsense["matches"] == [])
    hostile = await papi.search_projection(uuid.UUID(pid), q='" OR NEAR( AND *', method="bm25",
                                           k=3, current_user={"id": uid})
    check("bm25 survives FTS5 metacharacters", isinstance(hostile["matches"], list))

    # ── 11. clusters ───────────────────────────────────────────────────────
    print("\n11. clusters")
    clu = await worker.recalculate_clusters(str(pid))
    check("recalculate_clusters runs", clu["status"] == "ok", str(clu))
    async with acquire() as c:
        cl = await c.fetch(
            "SELECT cl.label, cl.member_count, cl.centroid, ch.path AS medoid_path "
            "FROM clusters cl LEFT JOIN chunks ch ON ch.id = cl.medoid_chunk_id "
            "WHERE cl.project_id = ? ORDER BY cl.member_count DESC", pid)
    check("cluster rows written", len(cl) == clu["clusters"], f"{len(cl)} vs {clu['clusters']}")
    if cl:
        check("centroid stored as a float32 blob",
              isinstance(cl[0]["centroid"], bytes) and len(cl[0]["centroid"]) == DIM * 4,
              f"{len(cl[0]['centroid'])} bytes")

    # ── 12. finalize ───────────────────────────────────────────────────────
    print("\n12. finalize_project")
    fin = await worker.finalize_project(str(pid), [str(did)])
    check("finalize sees a ready document", fin["status"] == "finalized", str(fin))
    async with acquire() as c:
        await c.execute("UPDATE documents SET status = ? WHERE id = ?",
                        json.dumps({"state": "parsing"}), did)
    fin2 = await worker.finalize_project(str(pid), [str(did)])
    check("finalize waits on a pending document",
          fin2["status"] == "waiting" and fin2["pending"] == 1, str(fin2))

    # ── 13. cascades ───────────────────────────────────────────────────────
    print("\n13. cascade delete")
    async with acquire() as c:
        gone = await c.fetchval(
            "DELETE FROM projects WHERE id = ? AND user_id = ? RETURNING id", pid, uid)
        left = {}
        for t in ("projects", "documents", "chunks", "messages", "clusters"):
            left[t] = await c.fetchval(f"SELECT count(*) FROM {t}")
        left["chunk_vectors"] = await c.fetchval("SELECT count(*) FROM chunk_vectors")
        left["chunks_fts"] = await c.fetchval("SELECT count(*) FROM chunks_fts")
    check("project delete RETURNs its id", gone is not None)
    check("cascade wiped every child", all(v == 0 for v in left.values()), str(left))

    await db.close_pool()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
