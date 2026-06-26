"""Per-file processing cache.

Each registered NetCDF file is processed once per content signature
(size, mtime). Processing pre-computes everything the map, 3D view,
variable / attribute panels and profile selector need, and pre-loads
the file's variable arrays into RAM so subsequent plot requests don't
re-open the netCDF.

A file's identity is the SHA-256 of its absolute path. Changing the
file on disk (or moving it) invalidates the cache. The lightweight
record (status, signature) is persisted to ~/.glider_playground/registry.json
so registered files survive a restart; payloads are rebuilt on demand.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import xarray as xr

from . import plot_logic
from . import spatial_logic
from . import derive_logic

# --- Configurable Variables ---
THROTTLE_PI_VARIABLES = 0.05
THROTTLE_PI_STAGES = 0.5
# ------------------------------

CACHE_ROOT = Path.home() / ".glider_playground"
UPLOADS_DIR = CACHE_ROOT / "uploads"
PAYLOADS_DIR = CACHE_ROOT / "payloads"
# On-demand cache of the packed binary plot payloads (what /api/plot_data?binary=1
# actually sends). Keyed by file signature + CACHE_VERSION + the output-affecting
# params, so a hit skips the whole read→filter→downsample→serialize pipeline.
PLOTCACHE_DIR = CACHE_ROOT / "plotcache"
REGISTRY_FILE = CACHE_ROOT / "registry.json"


def _resolve_data_dir() -> Path:
    """Where managed live-deployment downloads live and get auto-scanned.

    Picking this relative to the package only works for a source checkout — in
    the frozen desktop build the package lives *inside* the .app bundle, so the
    old default tried to write downloads into a read-only bundle. Resolution:

      1. ``GP_DATA_DIR`` env var, if set, always wins.
      2. A source checkout (``.git`` beside the package, not frozen) keeps using
         the repo's ``data/`` folder, so the dev workflow is unchanged.
      3. Everything else — a pip install or the frozen desktop app — uses a
         user-writable folder alongside our other state (``~/.glider_playground/data``).
    """
    env = os.environ.get("GP_DATA_DIR")
    if env:
        return Path(env).expanduser()

    repo_root = Path(__file__).resolve().parent.parent
    if not getattr(sys, "frozen", False) and (repo_root / ".git").is_dir():
        return repo_root / "data"

    return CACHE_ROOT / "data"


DATA_DIR = _resolve_data_dir()

# Bump this whenever processing logic changes and cached results should be
# invalidated (e.g. new QC algorithm, changed map generation, etc.).
# v9: Backscatter
# v10: binary plot payloads + on-demand plot-payload cache
CACHE_VERSION = "10"

# A file counts as NRT (Near Real-Time) if its last sample is within this
# window of "now" — anything fresher is presumed to still be deployed.
NRT_WINDOW_DAYS = 7

CACHE_ROOT.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)
PAYLOADS_DIR.mkdir(exist_ok=True)
PLOTCACHE_DIR.mkdir(exist_ok=True)


# Best-effort: ask glibc to return freed memory to the OS. On Linux/glibc
# this is the difference between RSS slowly creeping up across files and
# staying flat. No-op on macOS/musl.
try:
    import ctypes
    _libc = ctypes.CDLL("libc.so.6")
    def _malloc_trim():
        try:
            _libc.malloc_trim(0)
        except Exception:
            pass
except Exception:
    def _malloc_trim():
        pass


def _release_memory():
    gc.collect()
    _malloc_trim()

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_ERROR = "error"

# Per-record state we never persist: the in-flight Future and the cached
# JSON-ready payloads. Everything else is small metadata that's cheap to
# round-trip through registry.json.
_PAYLOAD_KEYS = ("map", "spatial_3d", "location",
                 "variables", "dataset_info", "profiles")
_TRANSIENT_KEYS = {"_future", "_done_steps", *_PAYLOAD_KEYS}

# Step identifiers used to skip work already completed before a crash.
STEP_PRELOAD = "preload"
STEP_DERIVE = "derive_ctd"
STEP_DATASET_INFO = "dataset_info"
STEP_PROFILES = "profiles"
STEP_SPATIAL = "spatial"
STEP_3D = "spatial_3d"
STEP_CTD_CLEAN = "ctd_clean"
STEP_CTD_INTERP = "ctd_interp"
STEP_CTD_BOTH = "ctd_both"
STEP_CTD_RECS = "ctd_recs"
ALL_STEPS = (STEP_PRELOAD, STEP_DERIVE, STEP_DATASET_INFO, STEP_PROFILES, STEP_SPATIAL,
             STEP_3D, STEP_CTD_CLEAN, STEP_CTD_INTERP, STEP_CTD_BOTH, STEP_CTD_RECS)

_lock = threading.RLock()
_registry: dict[str, dict] = {}
_executor = ThreadPoolExecutor(max_workers=1)
_loaded = False


# ---------- helpers ----------

def _file_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]


def _signature(path: Path) -> tuple[int, int]:
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


def _payload_path(file_id: str) -> Path:
    return PAYLOADS_DIR / f"{file_id}.json"


def _save_payload_sidecar(rec: dict):
    """Write the per-file sidecar containing payloads + done_steps + signature.

    Called after every step completes so a crash mid-file only loses the
    in-progress step. Best-effort: a write failure does not abort processing.
    """
    rid = rec.get("id")
    if not rid:
        return
    body = {
        "id": rid,
        "path": rec.get("path", ""),
        "size": rec.get("size", 0),
        "mtime": rec.get("mtime", 0),
        "cache_version": CACHE_VERSION,
        "done_steps": list(rec.get("_done_steps", [])),
        "ctd_interp_recommended": rec.get("ctd_interp_recommended", False),
        "ctd_clean_recommended": rec.get("ctd_clean_recommended", False),
        "last_time": rec.get("last_time"),
        "last_lat": rec.get("last_lat"),
        "last_lon": rec.get("last_lon"),
        "payloads": {k: rec[k] for k in _PAYLOAD_KEYS if k in rec},
    }
    tmp = _payload_path(rid).with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(body))
        os.replace(str(tmp), str(_payload_path(rid)))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _load_payload_sidecar(rec: dict) -> set:
    """Restore payloads from the sidecar if signature + version match.

    Returns the set of completed steps so the worker can skip them.
    """
    rid = rec.get("id")
    if not rid:
        return set()
    p = _payload_path(rid)
    if not p.exists():
        return set()
    try:
        body = json.loads(p.read_text())
    except Exception:
        return set()
    if body.get("cache_version") != CACHE_VERSION:
        return set()
    if (body.get("size"), body.get("mtime")) != (rec.get("size"), rec.get("mtime")):
        return set()
    for k, v in (body.get("payloads") or {}).items():
        rec[k] = v
    rec["ctd_interp_recommended"] = bool(body.get("ctd_interp_recommended", False))
    rec["ctd_clean_recommended"] = bool(body.get("ctd_clean_recommended", False))
    if body.get("last_time"): rec["last_time"] = body["last_time"]
    if body.get("last_lat") is not None: rec["last_lat"] = body["last_lat"]
    if body.get("last_lon") is not None: rec["last_lon"] = body["last_lon"]
    return set(body.get("done_steps") or [])


def _drop_payload_sidecar(file_id: str):
    try:
        _payload_path(file_id).unlink(missing_ok=True)
    except Exception:
        pass


def _persist_locked():
    """Write the registry to disk. Must be called with `_lock` held."""
    safe = {
        rid: {k: v for k, v in rec.items() if k not in _TRANSIENT_KEYS}
        for rid, rec in _registry.items()
    }
    safe["_cache_version"] = CACHE_VERSION
    try:
        REGISTRY_FILE.write_text(json.dumps(safe, indent=2))
    except Exception:
        pass


def _load_once():
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        if not REGISTRY_FILE.exists():
            return
        try:
            data = json.loads(REGISTRY_FILE.read_text())
        except Exception:
            return
        # If the processing code has changed, drop all cached results so every
        # file gets reprocessed with the new logic. Just bump CACHE_VERSION.
        if data.get("_cache_version") != CACHE_VERSION:
            _wipe_plotcache()   # stale binary payloads keyed by the old version
            return
        for rid, rec in data.items():
            if rid == "_cache_version":
                continue
            # Try to restore the per-file sidecar. If it matches signature
            # + version, the file is fully ready (or partially done) and we
            # can resume from where we left off without redoing work.
            done_steps = _load_payload_sidecar(rec)
            if done_steps:
                rec["_done_steps"] = list(done_steps)
                # Re-register the disk-backed preload sentinel so plot
                # endpoints find variables fast without a fresh xarray open.
                # Only meaningful in LOW_MEMORY mode — in RAM mode the
                # arrays are gone and plots fall back to opening NetCDF.
                if STEP_PRELOAD in done_steps and plot_logic._LOW_MEMORY:
                    try:
                        d = plot_logic._preload_dir(rec.get("path", ""))
                        if (d / "_names.json").exists():
                            with plot_logic._PRELOADED_LOCK:
                                plot_logic._PRELOADED[rec["path"]] = True
                        else:
                            # Disk preload is gone — must redo this step
                            done_steps.discard(STEP_PRELOAD)
                            rec["_done_steps"] = list(done_steps)
                    except Exception:
                        pass
                if set(done_steps) >= set(ALL_STEPS):
                    rec["status"] = STATUS_READY
                    rec["progress"] = 100
                    rec["stage"] = "ready"
                    rec["error"] = ""
                else:
                    rec["status"] = STATUS_PENDING
                    rec["progress"] = 0
                    rec["stage"] = "resuming"
                    rec["error"] = ""
            else:
                # No sidecar (or stale) — anything mid-flight at shutdown
                # becomes pending again and gets reprocessed from scratch.
                if rec.get("status") in (STATUS_PROCESSING, STATUS_READY):
                    rec["status"] = STATUS_PENDING
                    rec["progress"] = 0
                    rec["stage"] = "queued"
            _registry[rid] = rec


def _is_nrt(last_time_iso: Optional[str]) -> bool:
    """Computed dynamically so an old file ages out of NRT without reprocessing."""
    if not last_time_iso:
        return False
    try:
        import pandas as pd
        ts = pd.Timestamp(last_time_iso)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        delta = pd.Timestamp.utcnow().tz_convert("UTC") - ts.tz_convert("UTC")
        return delta.total_seconds() <= NRT_WINDOW_DAYS * 86400
    except Exception:
        return False


def _public_view(rec: dict) -> dict:
    last_time = rec.get("last_time")
    try:
        from . import live_logic
        is_managed = live_logic.is_managed(rec.get("path", ""))
    except Exception:
        is_managed = False
    return {
        "id": rec["id"],
        "name": rec["name"],
        "path": rec["path"],
        "size": rec.get("size", 0),
        "mtime": rec.get("mtime", 0),
        "status": rec.get("status", STATUS_PENDING),
        "progress": rec.get("progress", 0),
        "stage": rec.get("stage", ""),
        "error": rec.get("error", ""),
        "exists": Path(rec["path"]).exists(),
        "uploaded": rec.get("path", "").startswith(str(UPLOADS_DIR)),
        "ctd_interp_recommended": rec.get("ctd_interp_recommended", False),
        "ctd_clean_recommended": rec.get("ctd_clean_recommended", False),
        "last_time": last_time,
        "last_lat": rec.get("last_lat"),
        "last_lon": rec.get("last_lon"),
        "is_nrt": _is_nrt(last_time),
        "is_managed": is_managed,
    }


def _set(rec: dict, **fields):
    with _lock:
        rec.update(fields)


def _reset(rec: dict, size: int, mtime: int):
    """Mark a record as needing reprocessing. `_lock` must be held."""
    rec.update({
        "size": size, "mtime": mtime,
        "status": STATUS_PENDING, "progress": 0,
        "stage": "queued", "error": "",
    })
    for k in _PAYLOAD_KEYS:
        rec.pop(k, None)
    rec["_done_steps"] = []
    _drop_payload_sidecar(rec["id"])
    clear_plot_binary(rec["id"])
    plot_logic.clear_preloaded(rec.get("path", ""))
    spatial_logic.get_core_spatial_data.cache_clear()


def _enqueue(rec: dict):
    with _lock:
        fut = rec.get("_future")
        if fut is not None and not fut.done():
            return
        rec["_future"] = _executor.submit(_process, rec["id"])


def _refresh(rec: dict):
    """Detect on-disk changes / restart pending work."""
    p = Path(rec["path"])
    if not p.exists():
        with _lock:
            if rec.get("status") != STATUS_ERROR:
                rec.update(status=STATUS_ERROR, error="File no longer exists", progress=0)
                _persist_locked()
        return
    try:
        sig = _signature(p)
    except Exception:
        return
    with _lock:
        changed = (rec.get("size"), rec.get("mtime")) != sig
        if changed:
            _reset(rec, *sig)
            _persist_locked()
        needs_run = changed or rec.get("status") == STATUS_PENDING
    if needs_run:
        _enqueue(rec)


# ---------- public API ----------

def _scan_data_dir():
    """Register any .nc files in DATA_DIR not yet in the registry."""
    if not DATA_DIR.is_dir():
        return
    for p in sorted(DATA_DIR.rglob("*.nc")):
        rid = _file_id(p)
        with _lock:
            known = rid in _registry
        if not known:
            try:
                register_path(str(p))
            except Exception:
                pass


def list_files() -> list[dict]:
    _load_once()
    _scan_data_dir()
    with _lock:
        recs = list(_registry.values())
    for rec in recs:
        _refresh(rec)
    with _lock:
        return [_public_view(r) for r in _registry.values()]


def get_record(file_id: str) -> Optional[dict]:
    _load_once()
    with _lock:
        return _registry.get(file_id)


def resolve_path(file_id: str) -> Optional[str]:
    rec = get_record(file_id)
    return rec.get("path") if rec else None


def get_payload(file_id: str, key: str):
    """Pre-computed payload for a ready file, or None."""
    rec = get_record(file_id)
    if not rec or rec.get("status") != STATUS_READY:
        return None
    return rec.get(key)


def register_path(path: str) -> dict:
    p = Path(path).expanduser()
    try:
        p = p.resolve(strict=True)
    except FileNotFoundError as e:
        raise FileNotFoundError(str(p)) from e
    if not p.is_file():
        raise ValueError(f"Not a file: {p}")
    if p.suffix.lower() != ".nc":
        raise ValueError(f"Only .nc files supported, got {p.suffix}")

    rid = _file_id(p)
    size, mtime = _signature(p)

    _load_once()
    with _lock:
        rec = _registry.get(rid)
        if rec and (rec.get("size"), rec.get("mtime")) == (size, mtime) \
                and rec.get("status") == STATUS_READY:
            return _public_view(rec)
        if not rec:
            rec = {
                "id": rid, "name": p.name, "path": str(p),
                "size": size, "mtime": mtime,
                "status": STATUS_PENDING, "progress": 0,
                "stage": "queued", "error": "",
            }
            _registry[rid] = rec
        else:
            _reset(rec, size, mtime)
        _persist_locked()

    _enqueue(rec)
    return _public_view(rec)


def remove_file(file_id: str, *, delete_upload: bool = True) -> bool:
    _load_once()
    with _lock:
        rec = _registry.pop(file_id, None)
        if not rec:
            return False
        # Signal any in-flight worker to stop at its next checkpoint.
        rec["_removed"] = True
        # Cancel the future if it hasn't started yet.
        fut = rec.get("_future")
        if fut is not None:
            fut.cancel()
        path = rec.get("path", "")
        plot_logic.clear_preloaded(path)
        _drop_payload_sidecar(file_id)
        clear_plot_binary(file_id)
        _persist_locked()
    # Delete the upload file *after* releasing the lock so the worker's
    # open file handle can close cleanly before the path disappears.
    # Also delete from data/ when it's a file we ourselves downloaded —
    # otherwise it would just get re-scanned and re-processed on the next
    # `list_files()` call. Files a user manually placed in data/ are left
    # alone (only their cache record is dropped).
    if delete_upload:
        should_delete = path.startswith(str(UPLOADS_DIR))
        if not should_delete:
            try:
                from . import live_logic
                if live_logic.is_managed(path):
                    fname = Path(path).name
                    should_delete = True
                    # Also forget it in the live marker so the next scan
                    # treats it as "available to download" rather than
                    # "already managed".
                    try:
                        marker = live_logic._load_marker()
                        if fname in marker:
                            marker.pop(fname, None)
                            live_logic._save_marker(marker)
                    except Exception:
                        pass
                    # Suppress it so the auto-downloader doesn't immediately
                    # re-fetch the glider the user just deleted (binning =
                    # "stop auto-downloading this one").
                    try:
                        live_logic._add_suppressed(fname)
                    except Exception:
                        pass
            except Exception:
                pass
        if should_delete:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
    return True


def save_upload(name: str, content: bytes) -> dict:
    """Save uploaded bytes into the uploads dir, then register."""
    safe_name = Path(name).name or "uploaded.nc"
    target = UPLOADS_DIR / safe_name
    counter = 1
    stem, suffix = target.stem, target.suffix
    while target.exists():
        target = UPLOADS_DIR / f"{stem} ({counter}){suffix}"
        counter += 1
    target.write_bytes(content)
    return register_path(str(target))


def request_refresh(file_id: str) -> Optional[dict]:
    rec = get_record(file_id)
    if rec is None:
        return None
    _refresh(rec)
    return _public_view(rec)


# ---------- worker ----------

def _is_removed(rec: dict) -> bool:
    return rec.get("_removed", False)


_LOW_MEMORY = os.getenv("LOW_MEMORY_MODE", "").lower() in ("1", "true", "yes")


# ---------- binary plot-payload cache ----------
#
# A hit skips the whole get_plot_data_json pipeline (NetCDF read, CTD overlay,
# QC filter, downsample, pack) and returns the exact bytes we'd send. Two tiers:
# a small in-RAM LRU (hottest entries) over a per-file disk store that survives
# restarts. The disk key folds in the file signature + CACHE_VERSION, so a
# changed file or a version bump can never serve stale bytes.

# Keep RAM modest on the Pi (disk read of ~1.5 MB is only a few ms there); a
# roomier budget on a normal machine where repeated view loads benefit most.
_PLOTCACHE_MEM_MAX = (24 * 1024 * 1024) if _LOW_MEMORY else (256 * 1024 * 1024)
_PLOTCACHE_MEM: "OrderedDict[str, bytes]" = OrderedDict()
_PLOTCACHE_MEM_BYTES = 0
_PLOTCACHE_MEM_LOCK = threading.Lock()


def _plot_key(rec: dict, params_str: str) -> str:
    """Cache key = hash(version + file signature + output-affecting params)."""
    sig = f"{rec.get('size')}:{rec.get('mtime')}"
    return hashlib.sha256(f"{CACHE_VERSION}|{sig}|{params_str}".encode()).hexdigest()


def _plotcache_file(file_id: str, keyhash: str) -> Path:
    return PLOTCACHE_DIR / file_id / f"{keyhash}.bin"


def _mem_get(keyhash: str) -> Optional[bytes]:
    with _PLOTCACHE_MEM_LOCK:
        data = _PLOTCACHE_MEM.get(keyhash)
        if data is not None:
            _PLOTCACHE_MEM.move_to_end(keyhash)
        return data


def _mem_put(keyhash: str, data: bytes):
    if _PLOTCACHE_MEM_MAX <= 0 or len(data) > _PLOTCACHE_MEM_MAX:
        return
    global _PLOTCACHE_MEM_BYTES
    with _PLOTCACHE_MEM_LOCK:
        if keyhash in _PLOTCACHE_MEM:
            _PLOTCACHE_MEM_BYTES -= len(_PLOTCACHE_MEM.pop(keyhash))
        _PLOTCACHE_MEM[keyhash] = data
        _PLOTCACHE_MEM_BYTES += len(data)
        while _PLOTCACHE_MEM_BYTES > _PLOTCACHE_MEM_MAX and _PLOTCACHE_MEM:
            _, evicted = _PLOTCACHE_MEM.popitem(last=False)
            _PLOTCACHE_MEM_BYTES -= len(evicted)


def get_plot_binary(file_id: str, params_str: str) -> Optional[bytes]:
    """Cached packed binary for these params, or None. Only ready files are
    cached — mid-processing a derived var may be missing, which would poison
    the cache with a wrong (sparse) payload."""
    rec = get_record(file_id)
    if not rec or rec.get("status") != STATUS_READY:
        return None
    keyhash = _plot_key(rec, params_str)
    data = _mem_get(keyhash)
    if data is not None:
        return data
    fp = _plotcache_file(file_id, keyhash)
    try:
        if fp.exists():
            data = fp.read_bytes()
            _mem_put(keyhash, data)
            return data
    except Exception:
        pass
    return None


def put_plot_binary(file_id: str, params_str: str, data: bytes):
    rec = get_record(file_id)
    if not rec or rec.get("status") != STATUS_READY:
        return
    keyhash = _plot_key(rec, params_str)
    _mem_put(keyhash, data)
    fp = _plotcache_file(file_id, keyhash)
    tmp = fp.with_suffix(".bin.tmp")
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(str(tmp), str(fp))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def clear_plot_binary(file_id: str):
    """Drop a file's cached plot payloads (on reprocess / removal). The disk dir
    is keyed by file_id; the RAM tier isn't file-indexed, so on these rare events
    just clear it wholesale — it refills cheaply from disk/compute."""
    global _PLOTCACHE_MEM_BYTES
    shutil.rmtree(PLOTCACHE_DIR / file_id, ignore_errors=True)
    with _PLOTCACHE_MEM_LOCK:
        _PLOTCACHE_MEM.clear()
        _PLOTCACHE_MEM_BYTES = 0


def _wipe_plotcache():
    """Nuke the whole on-disk plot cache (e.g. on a CACHE_VERSION bump)."""
    global _PLOTCACHE_MEM_BYTES
    for child in PLOTCACHE_DIR.glob("*"):
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    with _PLOTCACHE_MEM_LOCK:
        _PLOTCACHE_MEM.clear()
        _PLOTCACHE_MEM_BYTES = 0


def _mark_step_done(rec: dict, step: str):
    """Mark a step complete and flush sidecar so a crash can resume after it."""
    with _lock:
        done = list(rec.get("_done_steps") or [])
        if step not in done:
            done.append(step)
        rec["_done_steps"] = done
        _save_payload_sidecar(rec)


def _process(file_id: str):
    rec = _registry.get(file_id)
    if rec is None:
        return
    p = rec["path"]
    if not Path(p).exists():
        _set(rec, status=STATUS_ERROR, error="File missing", progress=0, stage="error")
        with _lock:
            _persist_locked()
        return

    is_server = os.getenv("IS_SERVER") == "True"
    done_steps = set(rec.get("_done_steps") or [])

    def _is_done(step: str) -> bool:
        return step in done_steps

    try:
        # 1. Preload variable arrays. The preload cache is on disk in
        # LOW_MEMORY mode, so it survives a crash; we only redo it if the
        # sidecar says the step never finished.
        _set(rec, status=STATUS_PROCESSING, progress=15,
             stage="loading variables" if not _is_done(STEP_PRELOAD) else "resuming",
             error="")
        if not _is_done(STEP_PRELOAD):
            if _LOW_MEMORY:
                try:
                    plot_logic.stream_preload_to_disk(p, lambda: _is_removed(rec))
                except Exception as e:
                    raise RuntimeError(f"Failed to read NetCDF: {e}") from e
            else:
                all_vars: dict = {}
                try:
                    with plot_logic.NETCDF_LOCK, xr.open_dataset(p) as ds:
                        for name in ds.variables:
                            if _is_removed(rec):
                                return
                            try:
                                arr = ds.variables[name].values
                                all_vars[name] = arr.copy().ravel() if hasattr(arr, "ravel") else arr
                            except Exception:
                                pass
                    if _is_removed(rec):
                        return
                    plot_logic.set_preloaded(p, all_vars)
                    del all_vars
                except Exception as e:
                    raise RuntimeError(f"Failed to read NetCDF: {e}") from e
            _release_memory()
            _mark_step_done(rec, STEP_PRELOAD)
            done_steps.add(STEP_PRELOAD)
        else:
            # Make sure preload is registered in the in-RAM sentinel map even
            # though the actual arrays are still on disk from the previous run.
            if _LOW_MEMORY:
                d = plot_logic._preload_dir(p)
                if (d / "_names.json").exists():
                    with plot_logic._PRELOADED_LOCK:
                        plot_logic._PRELOADED[p] = True
                else:
                    # Disk preload was wiped between runs - redo it.
                    done_steps.discard(STEP_PRELOAD)
                    rec["_done_steps"] = [s for s in (rec.get("_done_steps") or []) if s != STEP_PRELOAD]
                    plot_logic.stream_preload_to_disk(p, lambda: _is_removed(rec))
                    _release_memory()
                    _mark_step_done(rec, STEP_PRELOAD)
                    done_steps.add(STEP_PRELOAD)

        if is_server:
            time.sleep(THROTTLE_PI_STAGES)

        # 1b. Derive extra variables.
        if _is_removed(rec):
            return
        if not _is_done(STEP_DERIVE):
            _set(rec, progress=28, stage="deriving variables")

            def _derive_cb(stage_msg: str):
                if not _is_removed(rec):
                    _set(rec, stage=stage_msg)

            try:
                derive_logic.derive_all_extra_variables(p, log_cb=_derive_cb)
            except Exception as e:
                print(f"Derivation failed for {p}: {e}")

            _release_memory()
            _mark_step_done(rec, STEP_DERIVE)
            done_steps.add(STEP_DERIVE)
            if is_server:
                time.sleep(THROTTLE_PI_STAGES)

        # 2. Dataset info + variables list.
        if _is_removed(rec):
            return
        if not _is_done(STEP_DATASET_INFO):
            _set(rec, progress=35, stage="indexing variables")
            rec["dataset_info"] = plot_logic.get_dataset_info(p)
            rec["variables"] = plot_logic.get_variables(p)
            _release_memory()
            _mark_step_done(rec, STEP_DATASET_INFO)
            done_steps.add(STEP_DATASET_INFO)

        # 3. Profiles.
        if _is_removed(rec):
            return
        if not _is_done(STEP_PROFILES):
            _set(rec, progress=50, stage="indexing profiles")
            rec["profiles"] = plot_logic.get_profiles(p)
            _release_memory()
            _mark_step_done(rec, STEP_PROFILES)
            done_steps.add(STEP_PROFILES)

        if is_server:
            time.sleep(THROTTLE_PI_STAGES)

        # 4. Spatial QC + map path + location.
        if _is_removed(rec):
            return
        if not _is_done(STEP_SPATIAL):
            _set(rec, progress=60, stage="spatial QC")

            def _spatial_cb(stage_msg: str):
                if not _is_removed(rec):
                    _set(rec, stage=stage_msg)

            spatial_logic._spatial_stage_cb = _spatial_cb
            try:
                rec["map"] = spatial_logic.generate_map_image(p)
                rec["location"] = spatial_logic.get_location_summary(p)
                endpt = spatial_logic.get_track_endpoint(p)
                if endpt:
                    rec["last_lat"] = endpt["last_lat"]
                    rec["last_lon"] = endpt["last_lon"]
                last_iso = spatial_logic.get_last_time_iso(p)
                if last_iso:
                    rec["last_time"] = last_iso
            finally:
                spatial_logic._spatial_stage_cb = None
            _release_memory()
            _mark_step_done(rec, STEP_SPATIAL)
            done_steps.add(STEP_SPATIAL)

        # 5. 3D + bathymetry.
        if _is_removed(rec):
            return
        if not _is_done(STEP_3D):
            _set(rec, progress=75, stage="fetching bathymetry")
            rec["spatial_3d"] = spatial_logic.generate_3d_data(p)
            _release_memory()
            _mark_step_done(rec, STEP_3D)
            done_steps.add(STEP_3D)

        # 6. Pre-warm CTD overlays - each combo is its own resumable step.
        # The overlay arrays themselves are cached to disk by plot_logic in
        # low-memory mode, so re-calling _ctd_processed_arrays after a
        # successful run is a cheap disk read.
        if _is_removed(rec):
            return
        ctd_plan = [
            (STEP_CTD_CLEAN,  False, True,  90, "pre-warming CTD: clean"),
            (STEP_CTD_INTERP, True,  False, 93, "pre-warming CTD: interpolate"),
            (STEP_CTD_BOTH,   True,  True,  96, "pre-warming CTD: interpolate + clean"),
        ]

        def _ctd_cb(stage_msg: str):
            if not _is_removed(rec):
                _set(rec, stage=stage_msg)

        plot_logic._ctd_stage_cb = _ctd_cb
        try:
            for step, interp, clean, pct, label in ctd_plan:
                if _is_removed(rec):
                    return
                if _is_done(step):
                    continue
                _set(rec, progress=pct, stage=label)
                plot_logic._ctd_processed_arrays(p, interp, clean)
                _release_memory()
                _mark_step_done(rec, step)
                done_steps.add(step)

            if not _is_done(STEP_CTD_RECS):
                _set(rec, progress=99, stage="checking CTD recommendations")
                try:
                    rec["ctd_interp_recommended"] = bool(plot_logic.ctd_interp_recommended(p))
                    rec["ctd_clean_recommended"] = bool(plot_logic.ctd_clean_recommended(p))
                except Exception:
                    rec["ctd_interp_recommended"] = False
                    rec["ctd_clean_recommended"] = False
                _mark_step_done(rec, STEP_CTD_RECS)
                done_steps.add(STEP_CTD_RECS)
        finally:
            plot_logic._ctd_stage_cb = None

        if _is_removed(rec):
            return
        _set(rec, status=STATUS_READY, progress=100, stage="ready",
             error="", processed_at=time.time())
        _save_payload_sidecar(rec)
        _release_memory()
    except Exception as e:
        if not _is_removed(rec):
            traceback.print_exc()
            _set(rec, status=STATUS_ERROR, error=f"{type(e).__name__}: {e}",
                 progress=0, stage="error")
    finally:
        with _lock:
            _persist_locked()