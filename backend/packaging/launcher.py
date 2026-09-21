"""Frozen-app entry point: one executable, two roles.

The desktop build ships a single binary that the shell starts twice —

    f7ows-backend serve     the FastAPI web process
    f7ows-backend worker    the job worker
    f7ows-backend selftest  prove the native dependencies survived freezing

rather than two separate executables. PyInstaller would otherwise duplicate the
whole Python runtime, numpy, scikit-learn and llvmlite into each one; sharing a
binary roughly halves the installer for two processes that import almost exactly
the same code.

This file exists only for the frozen build. In a source checkout the two commands
in COMMANDS_TO_START.txt (`uvicorn app.main:app` and `python -m app.worker`)
remain the way to run things, and nothing imports this module.
"""
from __future__ import annotations

import multiprocessing
import os
import sys


def _serve() -> None:
    import uvicorn

    # Import the app object rather than passing uvicorn the "app.main:app"
    # string. The string form resolves at runtime, so PyInstaller's static
    # analysis never sees the `app` package and silently omits it from the
    # bundle — the frozen binary then dies with ModuleNotFoundError: app.
    from app.main import app as fastapi_app

    # The shell picks a free port and passes it down; the default matches the dev
    # server so a frozen binary run by hand behaves like the checkout.
    host = os.getenv("F7OWS_HOST", "127.0.0.1")
    port = int(os.getenv("F7OWS_PORT", "8000"))
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


def _worker() -> None:
    import asyncio
    import logging

    from app.worker import _serve as serve_worker

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(serve_worker())
    except KeyboardInterrupt:
        pass


def _selftest() -> int:
    """Exercise every native dependency and report. Exit code 0 = all good.

    Freezing is where native payloads go missing, and each of these fails in a
    different, late, confusing way if its library did not make it into the
    bundle: no vector search, no projection map, no PDF text. They are cheap to
    check and expensive to debug in the field, so the shell can run this once at
    first launch (TODO_desktop.md phase 7) and refuse to show a broken UI.
    """
    import asyncio
    import io
    import traceback

    results: list[tuple[str, bool, str]] = []

    def probe(name, fn):
        try:
            results.append((name, True, str(fn())))
        except Exception:
            results.append((name, False, traceback.format_exc().strip().splitlines()[-1]))

    def sqlite_vec_roundtrip():
        """sqlite-vec's vec0.dll: loaded by path, so PyInstaller cannot see it."""
        from app import db

        async def go():
            conn = await db.create_pool()
            try:
                version = await conn.fetchval("SELECT vec_version()")
                await conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS _selftest_vec "
                    "USING vec0(id INTEGER PRIMARY KEY, embedding float[4])")
                await conn.execute(
                    "INSERT OR REPLACE INTO _selftest_vec(id, embedding) VALUES (1, ?)",
                    db.serialize_vector([1.0, 0.0, 0.0, 0.0]))
                d = await conn.fetchval(
                    "SELECT vec_distance_cosine(embedding, ?) FROM _selftest_vec WHERE id = 1",
                    db.serialize_vector([1.0, 0.0, 0.0, 0.0]))
                await conn.execute("DROP TABLE _selftest_vec")
                assert abs(d) < 1e-6, f"cosine distance to itself was {d}"
                return f"sqlite-vec {version}, self-distance {d:.1e}"
            finally:
                await db.close_pool()

        return asyncio.run(go())

    def numba_jit():
        """numba/llvmlite: the JIT compiles at runtime, so a broken llvmlite.dll
        only shows up the first time a projection is computed."""
        import numpy as np
        from numba import njit

        @njit(cache=False)
        def _add(a, b):
            return a + b

        return f"numba JIT ok ({_add(2.0, 3.0)})"

    def umap_projection():
        """The real thing: umap-learn on top of numba, which is what actually
        runs in recalculate_projections."""
        import numpy as np

        from app.projections import compute

        rng = np.random.default_rng(0)
        vectors = rng.normal(size=(40, 32)).astype(np.float32)
        coords = compute(vectors)
        got = {k: (None if v is None else v.shape) for k, v in coords.items()}
        assert coords["umap_2d"] is not None, "UMAP produced nothing"
        return str(got)

    def pdfium_parse():
        """pypdfium2's pdfium.dll: the parse path the app actually ships."""
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument.new()
        pdf.new_page(200, 200)
        buf = io.BytesIO()
        pdf.save(buf)
        reopened = pdfium.PdfDocument(buf.getvalue())
        return f"pypdfium2 ok, {len(reopened)} page(s)"

    def keyring_backend():
        """Phase 4 stores the API key here; a missing backend is silent until
        the user tries to save a key."""
        import keyring

        return type(keyring.get_keyring()).__name__

    probe("sqlite-vec", sqlite_vec_roundtrip)
    probe("numba JIT", numba_jit)
    probe("UMAP projection", umap_projection)
    probe("pypdfium2", pdfium_parse)
    probe("keyring", keyring_backend)

    width = max(len(n) for n, _, _ in results)
    failed = 0
    for name, ok, detail in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name:{width}}  {detail}")
        failed += not ok
    print()
    print(f"{len(results) - failed}/{len(results)} native dependencies working")
    return 1 if failed else 0


def main() -> int:
    # Required before anything spawns a process in a frozen build: without it a
    # child re-runs this module from the top instead of starting a worker, which
    # forks endlessly. Harmless when nothing spawns, so it goes first.
    multiprocessing.freeze_support()

    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if mode == "serve":
        _serve()
    elif mode == "worker":
        _worker()
    elif mode == "selftest":
        return _selftest()
    else:
        print(f"unknown mode {mode!r}; expected 'serve', 'worker' or 'selftest'",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
