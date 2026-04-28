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

import hashlib
import json
import threading
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import xarray as xr

from . import plot_logic
from . import spatial_logic

# --- Configurable Variables ---
THROTTLE_PI_VARIABLES = 0.05
THROTTLE_PI_STAGES = 0.5
# ------------------------------

CACHE_ROOT = Path.home() / ".glider_playground"
UPLOADS_DIR = CACHE_ROOT / "uploads"
REGISTRY_FILE = CACHE_ROOT / "registry.json"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Bump this whenever processing logic changes and cached results should be
# invalidated (e.g. new QC algorithm, changed map generation, etc.).
CACHE_VERSION = "2"

CACHE_ROOT.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_ERROR = "error"

# Per-record state we never persist: the in-flight Future and the cached
# JSON-ready payloads. Everything else is small metadata that's cheap to
# round-trip through registry.json.
_PAYLOAD_KEYS = ("map", "spatial_3d", "location",
                 "variables", "dataset_info", "profiles")
_TRANSIENT_KEYS = {"_future", *_PAYLOAD_KEYS}

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
            return
        for rid, rec in data.items():
            if rid == "_cache_version":
                continue
            # Anything mid-flight at shutdown becomes pending again.
            if rec.get("status") in (STATUS_PROCESSING, STATUS_READY):
                rec["status"] = STATUS_PENDING
                rec["progress"] = 0
                rec["stage"] = "queued"
            _registry[rid] = rec


def _public_view(rec: dict) -> dict:
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
        _persist_locked()
    # Delete the upload file *after* releasing the lock so the worker's
    # open file handle can close cleanly before the path disappears.
    if delete_upload and path.startswith(str(UPLOADS_DIR)):
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

    try:
        # 1. Load the entire dataset into RAM.
        _set(rec, status=STATUS_PROCESSING, progress=15,
             stage="loading variables", error="")
        all_vars: dict = {}
        try:
            with xr.open_dataset(p) as ds:
                for name in ds.variables:
                    if _is_removed(rec):
                        return
                    try:
                        arr = ds.variables[name].values
                        all_vars[name] = arr.copy().ravel() if hasattr(arr, "ravel") else arr
                    except Exception:
                        pass

                    if is_server:
                        time.sleep(THROTTLE_PI_VARIABLES)

            if _is_removed(rec):
                return
            plot_logic.set_preloaded(p, all_vars)
        except Exception as e:
            raise RuntimeError(f"Failed to read NetCDF: {e}") from e

        if is_server:
            time.sleep(THROTTLE_PI_STAGES)

        # 2. Variables / attributes / profiles.
        if _is_removed(rec):
            return
        _set(rec, progress=35, stage="indexing variables")
        rec["dataset_info"] = plot_logic.get_dataset_info(p)
        rec["variables"] = plot_logic.get_variables(p)

        if is_server:
            time.sleep(THROTTLE_PI_STAGES)

        if _is_removed(rec):
            return
        _set(rec, progress=50, stage="indexing profiles")
        rec["profiles"] = plot_logic.get_profiles(p)

        if is_server:
            time.sleep(THROTTLE_PI_STAGES)

        # 3. Spatial QC + map path.
        if _is_removed(rec):
            return
        _set(rec, progress=60, stage="spatial QC")

        def _spatial_cb(stage_msg: str):
            if not _is_removed(rec):
                _set(rec, stage=stage_msg)

        spatial_logic._spatial_stage_cb = _spatial_cb
        try:
            rec["map"] = spatial_logic.generate_map_image(p)
            rec["location"] = spatial_logic.get_location_summary(p)
        finally:
            spatial_logic._spatial_stage_cb = None

        if is_server:
            time.sleep(THROTTLE_PI_STAGES)

        # 4. 3D + bathy.
        if _is_removed(rec):
            return
        _set(rec, progress=75, stage="fetching bathymetry")
        rec["spatial_3d"] = spatial_logic.generate_3d_data(p)

        if is_server:
            time.sleep(THROTTLE_PI_STAGES)

        # 5. Pre-warm CTD overlays. The stage callback lets _apply_ctd_processing
        #    push fine-grained sub-step names into rec.stage in real time so the
        #    user sees exactly what's happening (e.g. "CTD interp: filling 12k
        #    TEMP gaps") instead of a static label for the whole slow pass.
        if _is_removed(rec):
            return
        ctd_steps = [
            (False, True,  90, "pre-warming CTD: clean"),
            (True,  False, 93, "pre-warming CTD: interpolate"),
            (True,  True,  96, "pre-warming CTD: interpolate + clean"),
        ]

        def _ctd_cb(stage_msg: str):
            if not _is_removed(rec):
                _set(rec, stage=stage_msg)

        plot_logic._ctd_stage_cb = _ctd_cb
        try:
            for interp, clean, pct, label in ctd_steps:
                if _is_removed(rec):
                    return
                _set(rec, progress=pct, stage=label)
                plot_logic._ctd_processed_arrays(p, interp, clean)

                if is_server:
                    time.sleep(THROTTLE_PI_STAGES)

            _set(rec, progress=99, stage="checking CTD recommendations")
            rec["ctd_interp_recommended"] = bool(plot_logic.ctd_interp_recommended(p))
            rec["ctd_clean_recommended"] = bool(plot_logic.ctd_clean_recommended(p))
        except Exception:
            rec["ctd_interp_recommended"] = False
            rec["ctd_clean_recommended"] = False
        finally:
            plot_logic._ctd_stage_cb = None

        if _is_removed(rec):
            return
        _set(rec, status=STATUS_READY, progress=100, stage="ready",
             error="", processed_at=time.time())
    except Exception as e:
        if not _is_removed(rec):
            traceback.print_exc()
            _set(rec, status=STATUS_ERROR, error=f"{type(e).__name__}: {e}",
                 progress=0, stage="error")
    finally:
        with _lock:
            _persist_locked()