# PyInstaller spec for the F7ows Python backend.
#
#   cd backend && pyinstaller packaging/f7ows-backend.spec --noconfirm
#
# Produces dist/f7ows-backend/, a onedir bundle the Tauri shell ships as a
# sidecar and starts twice (`serve` and `worker`, see packaging/launcher.py).
#
# onedir, not onefile: a onefile build unpacks the whole bundle to a temp
# directory on every launch, which for a ~450 MB payload means a visible delay
# each time the app starts, twice over for two processes. onedir starts
# immediately and lets the installer do the extracting once.
#
# The interesting part of this file is what it EXCLUDES. See `excludes` below.

import os
import sys

from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                     collect_submodules)

# backend/, i.e. the directory holding the `app` package. SPECPATH is the folder
# containing this file (backend/packaging), and PyInstaller sets it regardless of
# where the build was invoked from — a relative ".." here would resolve against
# the caller's working directory instead, which is how the first build silently
# produced a bundle with no `app` package in it at all.
BACKEND_DIR = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# collect_submodules() imports the package to walk it, so the path has to be
# live in *this* process too, not just recorded in pathex for the analysis.
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Native libraries that ship inside their packages. PyInstaller finds ordinary
# extension modules by itself, but these are data-ish payloads loaded at runtime
# by path or by ctypes, so they have to be named:
#   vec0.dll      loaded via sqlite3's load_extension (app/db.py)
#   pdfium.dll    loaded by pypdfium2_raw through ctypes
#   llvmlite.dll  numba's JIT backend, loaded by ctypes
binaries = (
    collect_dynamic_libs("sqlite_vec")
    + collect_dynamic_libs("pypdfium2_raw")
    + collect_dynamic_libs("llvmlite")
)

datas = collect_data_files("sqlite_vec")

hiddenimports = [
    # uvicorn resolves its protocol/loop implementations by string name, so
    # nothing imports them statically and PyInstaller cannot see them.
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.loops.asyncio",
    "uvicorn.logging",
    # Imported lazily inside functions in app/worker.py.
    "sklearn.utils._typedefs",
    "sklearn.neighbors._partition_nodes",
    # keyring picks its backend at runtime by entry point; on Windows that is
    # the Credential Manager one. (app/keystore.py — phase 4)
    "keyring.backends.Windows",
]

# Our own package, in full. launcher.py imports app.main and app.worker
# explicitly, but several submodules are imported lazily inside functions
# (app.projections.compute and app.clusters in the worker's recompute jobs,
# app.llm inside the retrieval router) and static analysis reaches none of them.
# A missing one would surface as a crash in the middle of an ingest rather than
# at startup, so the whole package goes in.
hiddenimports += collect_submodules("app")
assert any(m == "app.main" for m in hiddenimports), (
    "the `app` package was not found from %s — the bundle would build fine and "
    "then die at startup with ModuleNotFoundError" % BACKEND_DIR)

excludes = [
    # The big one. `docling` gives the high-fidelity parse path, but it pulls
    # torch + torchvision + transformers + accelerate — ~700 MB of wheels before
    # any model weights. Nothing in the app reaches it: parse() defaults to
    # use_models=False and no caller passes True. Per TODO_desktop.md phase 7 it
    # is an opt-in download, not something every user installs, so it must not
    # be dragged in even if it happens to be present in the build environment.
    "docling", "docling_core", "docling_parse", "docling_ibm_models",
    "torch", "torchvision", "transformers", "accelerate", "safetensors",
    "huggingface_hub", "tokenizers", "cv2", "rapidocr",
    # Dev/test-only, and matplotlib in particular is large and pulled in
    # speculatively by scientific packages.
    "matplotlib", "pytest", "IPython", "notebook", "tkinter",
    "tiktoken",          # dev-only calibration harness (app/tokens.py)
    # Retired by the port; excluded so a stale build environment can't
    # resurrect them into the bundle.
    "asyncpg", "psycopg", "psycopg2", "sqlalchemy", "alembic", "arq",
    "boto3", "botocore", "redis",
]

a = Analysis(
    ["launcher.py"],
    pathex=[BACKEND_DIR],    # so `import app.*` resolves
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="f7ows-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX mangles some numpy/llvmlite DLLs
    console=True,            # the shell captures stdout/stderr for the log files
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="f7ows-backend",
)
