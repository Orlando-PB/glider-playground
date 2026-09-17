"""Per-file processing cache.

Each registered NetCDF file is processed once per content signature (size,
mtime) and CACHE_VERSION. Processing pre-computes what the map, 3D view,
variable panels and profile selector need, and pre-loads the file's variable
arrays so plot requests don't re-open the NetCDF. The arrays go to ~/.glider_playground/preload/ as .npy and are memory-mapped per
request (only the variables asked for), so RAM stays flat as files are added
and a restart is warm. Same locally and on the server. Disk cost is roughly
the file's uncompressed size.

Also holds the plot-response cache (small RAM LRU over a per-file disk store)
and the default-plot prewarm driven by plot_presets.json.

A file's id is the SHA-256 of its absolute path. The registry persists to
~/.glider_playground/registry.json and finished payloads to a per-file
sidecar, so a restart resumes rather than reprocesses.
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import os
import shutil
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
from . import presets_logic
from ..server import server_config

logger = logging.getLogger(__name__)

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

      1. ``GP_DATA_DIR`` env var, if set, always wins.
      2. A source checkout (``.git`` beside the package) keeps using the repo's
         ``data/`` folder, so the dev workflow is unchanged.
      3. Everything else — a pip install — uses a user-writable folder
         alongside our other state (``~/.glider_playground/data``).
    """
    env = os.environ.get("GP_DATA_DIR")
    if env:
        return Path(env).expanduser()

    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / ".git").is_dir():
        return repo_root / "data"

    return CACHE_ROOT / "data"


DATA_DIR = _resolve_data_dir()

# Part of every cache key: bump when a processing change alters cached output
# (history: OVERVIEW.md, "Cache version history").
CACHE_VERSION = "31"

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


def _lower_worker_priority():
    """Renice the calling (worker) thread so request-serving always wins the CPU.

    On the Pi the processing worker runs heavy numpy (CTD prewarm, derivation)
    that otherwise saturates the core and starves uvicorn — static files take
    seconds and the proxy starts returning 502s. On Linux nice is per-thread, so
    this only deprioritises the worker, not the server. setpriority is absolute
    (unlike os.nice's relative increment) so it's safe to call once per file.
    Skipped off-server: on macOS niceness is per-process, and the local
    box has spare cores anyway, so we never want to slow it down.
    """
    if not server_config.IS_SERVER:
        return
    try:
        os.setpriority(os.PRIO_PROCESS, 0, 10)
    except (AttributeError, OSError):
        pass

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
# Best-effort prewarm of the default plot payloads — deliberately NOT in ALL_STEPS
# so "ready" never waits on it (the file is fully usable without it; a missing
# prewarm just means the first click computes live, as before).
STEP_PLOT_PREWARM = "plot_prewarm"
ALL_STEPS = (STEP_PRELOAD, STEP_DERIVE, STEP_DATASET_INFO, STEP_PROFILES, STEP_SPATIAL,
             STEP_3D, STEP_CTD_CLEAN, STEP_CTD_INTERP, STEP_CTD_BOTH)

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
        "last_time": rec.get("last_time"),
        "last_lat": rec.get("last_lat"),
        "last_lon": rec.get("last_lon"),
        "platform_kind": rec.get("platform_kind"),
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
    if body.get("last_time"): rec["last_time"] = body["last_time"]
    if body.get("last_lat") is not None: rec["last_lat"] = body["last_lat"]
    if body.get("last_lon") is not None: rec["last_lon"] = body["last_lon"]
    if body.get("platform_kind") is not None: rec["platform_kind"] = body["platform_kind"]
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


def _known_hashes() -> set:
    """Every 16-hex name a registered file's on-disk caches can be stored under:
    its id (resolved path) and the hash of its path as recorded."""
    with _lock:
        recs = list(_registry.items())
    known = set()
    for rid, rec in recs:
        known.add(rid)
        if rec.get("path"):
            known.add(hashlib.sha256(rec["path"].encode()).hexdigest()[:16])
    return known


def _sweep_orphans():
    """Delete on-disk caches belonging to files that are no longer registered.

    Per-file cleanup only runs when a file is removed or changes while the app is
    up; a moved/renamed file (new path → new hash), a reset registry, or an old
    install's leftovers would otherwise sit in preload/, derived/, ctd_cache/,
    payloads/ and plotcache/ forever. Everything there is regenerable. Only called
    after the registry has loaded successfully — never on a missing/corrupt one —
    and runs in the background since it can be many GB.
    """
    known = _known_hashes()
    orphans = []
    for d in (plot_logic._PRELOAD_CACHE_DIR, plot_logic._DERIVED_CACHE_DIR,
              plot_logic._CTD_CACHE_DIR, PAYLOADS_DIR, PLOTCACHE_DIR):
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for e in entries:
            h = e.name[:16]
            # Only ever touch names that look like ours: <16 hex>[_...|.ext]
            if len(h) == 16 and all(c in "0123456789abcdef" for c in h) and h not in known:
                orphans.append((e, h))
    if not orphans:
        return

    def _run():
        freed = 0
        for e, h in orphans:
            if h in _known_hashes():   # registered while we were sweeping
                continue
            try:
                if e.is_dir():
                    freed += sum(f.stat().st_size for f in e.rglob("*") if f.is_file())
                    shutil.rmtree(e, ignore_errors=True)
                else:
                    freed += e.stat().st_size
                    e.unlink(missing_ok=True)
            except OSError:
                pass
        logger.warning("Cache sweep: removed %d orphaned cache entries (%.1f GB) for files "
                       "no longer registered", len(orphans), freed / 1e9)

    threading.Thread(target=_run, name="cache-sweep", daemon=True).start()


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
        # CACHE_VERSION changed: drop cached results but keep the records (the user's file list,
        # including files registered by path, which a rescan could not recover).
        stale_version = data.get("_cache_version") != CACHE_VERSION
        if stale_version:
            _wipe_plotcache()   # stale binary payloads keyed by the old version
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
                if STEP_PRELOAD in done_steps:
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
            if stale_version or rec.get("status") == STATUS_ERROR:
                # Retry errored files on every start: a bump may be the very
                # fix they were waiting for, and plenty of errors are transient
                # (a network blip fetching bathymetry, an import-lock deadlock
                # under a busy startup). A genuinely broken file just errors
                # again quickly, so this costs nothing.
                rec["status"] = STATUS_PENDING
                rec["progress"] = 0
                rec["stage"] = "queued"
                rec["error"] = ""
            _registry[rid] = rec
        _persist_locked()
    _sweep_orphans()


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



# ---------- platform kind (icon) ----------

# NERC B76 platform-vocabulary codes seen in BODC OG1 files → icon kind.
_B76_KIND = {
    "B7600001": "slocum",     # Teledyne Webb Research Slocum G2 glider
    "B7600029": "slocum",     # Teledyne Webb Research Slocum G3S glider
    "B7600002": "seaglider",  # Kongsberg Seaglider
    "B7600021": "alr",        # NOC Autosub Long Range 1500
}
PLATFORM_KINDS = ("slocum", "seaglider", "alr")


def _detect_platform_kind(path: str) -> str:
    """'slocum' | 'seaglider' | 'alr' | '' from the file's global attributes.

    Cheap (attrs only, no data read). Drives the platform icon shown on file
    cards and map markers; '' means "unknown, use the generic marker".
    """
    try:
        with xr.open_dataset(path, decode_times=False, decode_cf=False) as ds:
            attrs = {str(k).lower(): str(v) for k, v in ds.attrs.items()}
    except Exception:
        return ""
    vocab = attrs.get("platform_vocabulary", "")
    for code, kind in _B76_KIND.items():
        if code in vocab:
            return kind
    text = " ".join(attrs.get(k, "") for k in
                    ("platform_type", "platform_model", "platform", "instrument", "id", "title", "source")).lower()
    text += " " + Path(path).name.lower()
    if "slocum" in text:
        return "slocum"
    if "seaglider" in text or "sea glider" in text:
        return "seaglider"
    if "autosub" in text or "alr" in text.replace("_", " ").split() or Path(path).name.lower().startswith("alr"):
        return "alr"
    return ""


def _ensure_platform_kind(rec: dict):
    """Fill ``platform_kind`` once for ready records that predate the field."""
    if rec.get("status") != STATUS_READY or "platform_kind" in rec:
        return
    kind = _detect_platform_kind(rec.get("path", ""))
    with _lock:
        rec["platform_kind"] = kind
        _persist_locked()


def _public_view(rec: dict) -> dict:
    last_time = rec.get("last_time")
    try:
        from ..server import erddap_fetch
        is_managed = erddap_fetch.is_managed(rec.get("path", ""))
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
        "last_time": last_time,
        "last_lat": rec.get("last_lat"),
        "last_lon": rec.get("last_lon"),
        "is_nrt": _is_nrt(last_time),
        "is_managed": is_managed,
        "platform_kind": rec.get("platform_kind") or "",
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
        # File deleted on disk: drop the record and its caches. Live marker/suppress lists are
        # left alone (that's remove_file's job for a user-initiated delete).
        with _lock:
            if _registry.pop(rec["id"], None) is None:
                return
            rec["_removed"] = True
            fut = rec.get("_future")
            if fut is not None:
                fut.cancel()
            plot_logic.clear_preloaded(rec.get("path", ""))
            _drop_payload_sidecar(rec["id"])
            clear_plot_binary(rec["id"])
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
    """Register any .nc files not yet in the registry.

    Sweeps both DATA_DIR (live/BODC downloads) and UPLOADS_DIR (admin-panel
    uploads). Uploads must be scanned here too: a CACHE_VERSION bump drops the
    whole registry (see _load_once), and only files re-discovered by this scan
    come back — so without UPLOADS_DIR, uploaded files would be silently
    orphaned on disk and never reprocessed.
    """
    seen = set()
    for base in (DATA_DIR, UPLOADS_DIR):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.nc")):
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            rid = _file_id(p)
            with _lock:
                known = rid in _registry
            if not known:
                try:
                    register_path(str(p))
                except Exception:
                    pass


# Throttle the on-disk sweep: the frontend polls list_files() every ~1 s while processing, and a
# full rescan per poll starves the worker on the Pi. Progress still updates live between sweeps.
_SCAN_INTERVAL_S = 3.0
_last_scan = 0.0


def list_files() -> list[dict]:
    global _last_scan
    _load_once()
    now = time.monotonic()
    if now - _last_scan >= _SCAN_INTERVAL_S:
        _last_scan = now
        _scan_data_dir()
        with _lock:
            recs = list(_registry.values())
        for rec in recs:
            _refresh(rec)
            _ensure_platform_kind(rec)
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
    # Delete after releasing the lock so the worker's file handle closes first. Files we downloaded are
    # removed from data/ too (else they'd be rescanned); files the user placed there are kept.
    if delete_upload:
        should_delete = path.startswith(str(UPLOADS_DIR))
        if not should_delete:
            try:
                from ..server import erddap_fetch
                if erddap_fetch.is_managed(path):
                    fname = Path(path).name
                    should_delete = True
                    # Also forget it in the live marker so the next scan
                    # treats it as "available to download" rather than
                    # "already managed".
                    try:
                        marker = erddap_fetch._load_marker()
                        if fname in marker:
                            marker.pop(fname, None)
                            erddap_fetch._save_marker(marker)
                    except Exception:
                        pass
                    # Suppress it so the auto-downloader doesn't immediately
                    # re-fetch the glider the user just deleted (binning =
                    # "stop auto-downloading this one").
                    try:
                        erddap_fetch._add_suppressed(fname)
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


# ---------- binary plot-payload cache ----------
# Exact response bytes, in a small RAM LRU over a per-file disk store. Keys fold in the file
# signature + CACHE_VERSION, so a changed file or version bump never serves stale bytes.

# Modest on purpose: a miss is only a disk read of ~1.5 MB (a few ms, even on the Pi).
_PLOTCACHE_MEM_MAX = 64 * 1024 * 1024
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
    # The overlay prefetch store lives in this dir too — drop its bookkeeping.
    from ..maps import copernicus_prefetch   # lazy: it imports this module
    copernicus_prefetch.forget(file_id)


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


# --- Default-plot prewarm ---
# Must mirror the frontend's first request (presets with prewarm: true, _ADJUSTED preferred,
# auto-detected cycle var) so the cached key matches. A mismatch only wastes work; it can't serve wrong data.


def _resolve_first(candidates, var_set):
    """Mirror of the frontend's findBest: first candidate present in the file,
    preferring its _ADJUSTED variant."""
    for v in candidates:
        adj = f"{v}_ADJUSTED"
        if adj in var_set:
            return adj
        if v in var_set:
            return v
    return None


def _prewarm_default_plots(rec: dict):
    """Pre-pack the binary plot payloads for the common default views so the first
    click is a cache hit instead of a cold read→ctd→filter→pack. Runs after the
    file is READY (so put_plot_binary will store) and is best-effort throughout."""
    from . import cycle_profile_logic  # local import: avoids any import-order cycle

    path = rec.get("path")
    if not path or not Path(path).exists():
        return
    var_names = set(plot_logic._get_var_names(path) or [])
    if not var_names:
        return

    qc_flags = "0,1,2,5,8"

    try:
        cyc = cycle_profile_logic.get_cycles(path)
        cycle_var = cyc.get("cycle_var") if isinstance(cyc, dict) else None
    except Exception:
        cycle_var = None

    is_server = server_config.IS_SERVER
    # Match the frontend's Auto budget: 60k on the server, 100k locally.
    max_points = 60000 if is_server else 100000

    # One (x, y, c) per prewarm-flagged preset the file can satisfy.
    combos = []
    for xc, yc, cc in presets_logic.prewarm_candidates():
        combo = (_resolve_first(xc, var_names), _resolve_first(yc, var_names), _resolve_first(cc, var_names))
        if all(combo) and combo not in combos:
            combos.append(combo)
    if not combos:
        return

    # The frontend may or may not have loaded /api/cycles before firing its first
    # plot, so the request key can carry cycle_var or omit it. With cycle_num=None
    # the cycle_var changes nothing in the data, so we pack once and store under
    # both keys — a guaranteed hit either way.
    cycle_var_keys = [None] if cycle_var is None else [cycle_var, None]

    for x_var, y_var, c_var in combos:
        if _is_removed(rec):
            return
        try:
            result = plot_logic.get_plot_data_json(
                path, x_var, y_var, c_var,
                qc_flags=qc_flags,
                profile_num=None,
                cycle_num=None, cycle_var=cycle_var, sci_phases=None, direction_filter=None,
                highlight_profile=False,
                max_points=max_points, binary=True,
            )
        except Exception:
            continue
        if not isinstance(result, (bytes, bytearray)):
            continue  # an error dict — skip (don't poison the cache)
        data = bytes(result)
        for cv in cycle_var_keys:
            params_str = plot_logic.plot_cache_params_str(
                x_var=x_var, y_var=y_var, c_var=c_var, qc_flags=qc_flags,
                profile_num=None,
                cycle_num=None, cycle_var=cv, sci_phases="", direction_filter="",
                highlight_profile=False,
                max_points=max_points, zoom_x_var=None, zoom_x_min=None, zoom_x_max=None,
                zoom_y_min=None, zoom_y_max=None,
            )
            put_plot_binary(rec["id"], params_str, data)
        _release_memory()
        if is_server:
            time.sleep(THROTTLE_PI_VARIABLES)


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

    is_server = server_config.IS_SERVER
    _lower_worker_priority()
    done_steps = set(rec.get("_done_steps") or [])

    def _is_done(step: str) -> bool:
        return step in done_steps

    try:
        # 1. Preload variable arrays to disk (.npy). They survive a crash or
        # restart; we only redo it if the sidecar says the step never finished
        # or the preload dir has been wiped.
        _set(rec, status=STATUS_PROCESSING, progress=15,
             stage="loading variables" if not _is_done(STEP_PRELOAD) else "resuming",
             error="")
        preload_on_disk = (plot_logic._preload_dir(p) / "_names.json").exists()
        if _is_done(STEP_PRELOAD) and preload_on_disk:
            with plot_logic._PRELOADED_LOCK:
                plot_logic._PRELOADED[p] = True
        else:
            done_steps.discard(STEP_PRELOAD)
            rec["_done_steps"] = [s for s in (rec.get("_done_steps") or []) if s != STEP_PRELOAD]
            try:
                plot_logic.stream_preload_to_disk(p, lambda: _is_removed(rec))
            except Exception as e:
                raise RuntimeError(f"Failed to read NetCDF: {e}") from e
            if _is_removed(rec):
                return
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
                logger.warning("Derivation failed for %s: %s", p, e)

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
                rec["platform_kind"] = _detect_platform_kind(str(p))
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
        # The overlay arrays themselves are cached to disk by plot_logic,
        # so re-calling _ctd_processed_arrays after a
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

        finally:
            plot_logic._ctd_stage_cb = None

        if _is_removed(rec):
            return
        _set(rec, status=STATUS_READY, progress=100, stage="ready",
             error="", processed_at=time.time())
        _save_payload_sidecar(rec)
        _release_memory()

        # Best-effort: pre-pack the default plot payloads now that the file is READY
        # (put_plot_binary only stores for ready files) so the first click is a cache
        # hit. Failures here never affect the ready state.
        if not _is_done(STEP_PLOT_PREWARM) and not _is_removed(rec):
            try:
                _prewarm_default_plots(rec)
            except Exception:
                traceback.print_exc()
            _mark_step_done(rec, STEP_PLOT_PREWARM)
            _release_memory()

        # Hand off to the overlay prefetch worker (its own thread, so Copernicus
        # network time never holds up the next file's processing).
        if not _is_removed(rec):
            try:
                from ..maps import copernicus_prefetch
                copernicus_prefetch.ensure(file_id)
            except Exception:
                traceback.print_exc()
    except Exception as e:
        if not _is_removed(rec):
            traceback.print_exc()
            _set(rec, status=STATUS_ERROR, error=f"{type(e).__name__}: {e}",
                 progress=0, stage="error")
    finally:
        with _lock:
            _persist_locked()