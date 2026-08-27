"""Live glider feed.

Scans an ERDDAP file index for files updated within the past `DAYS_ACTIVE`
days, downloads them into `DATA_DIR`, and tracks ownership in a marker file
so that deletes only ever touch files we wrote — never user-placed data.

Designed to run on a small server (Raspberry Pi) shared between users:
  * The ERDDAP scan result is cached in-process for `SCAN_CACHE_TTL` seconds
    so concurrent clients share one upstream fetch.
  * Downloads are serialised on a single background worker.
  * Auto-update of locally-managed files runs as a side-effect of `list_live`
    but is rate-limited and never blocks the response.
  * Files that have aged past `DAYS_ACTIVE` are pruned automatically.
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests

from . import cache_logic

SERVER_FILES_URL = "https://linkedsystems.uk/erddap/files/"
DAYS_ACTIVE = 7
FILE_SUFFIX = "_R.nc"
SCAN_CACHE_TTL = 120          # seconds — 2 min server-side cache for the listing
AUTO_UPDATE_COOLDOWN = 300    # seconds — minimum gap between auto-update sweeps
HTTP_TIMEOUT = 15

MARKER_FILE = cache_logic.DATA_DIR / ".glider_playground_managed.json"
# Gliders the user "binned": never auto-download these again until they ask
# for one explicitly (a manual download clears the suppression).
SUPPRESS_FILE = cache_logic.DATA_DIR / ".glider_playground_suppressed.json"
SCANNER_INTERVAL = 1800       # seconds — background re-scan to pick up new gliders

_lock = threading.RLock()
_scan_cache: dict = {"at": 0.0, "data": None}
_last_auto_update: float = 0.0
_in_flight: set[str] = set()  # filenames currently downloading
_scanner_started = False
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-dl")


# ---------- managed-file marker ----------

def _load_marker() -> dict:
    """Map filename → {"server_mtime": float, "downloaded_at": float}."""
    if not MARKER_FILE.exists():
        return {}
    try:
        return json.loads(MARKER_FILE.read_text()) or {}
    except Exception:
        return {}


def _save_marker(data: dict):
    try:
        cache_logic.DATA_DIR.mkdir(parents=True, exist_ok=True)
        MARKER_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ---------- suppressed (binned) list ----------

def _load_suppressed() -> set:
    """Filenames the user removed and that must not be auto-downloaded again."""
    if not SUPPRESS_FILE.exists():
        return set()
    try:
        data = json.loads(SUPPRESS_FILE.read_text())
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def _save_suppressed(names: set):
    try:
        cache_logic.DATA_DIR.mkdir(parents=True, exist_ok=True)
        SUPPRESS_FILE.write_text(json.dumps(sorted(names), indent=2))
    except Exception:
        pass


def _add_suppressed(filename: str):
    with _lock:
        s = _load_suppressed()
        if filename not in s:
            s.add(filename)
            _save_suppressed(s)


def _remove_suppressed(filename: str):
    with _lock:
        s = _load_suppressed()
        if filename in s:
            s.discard(filename)
            _save_suppressed(s)


def is_suppressed(filename: str) -> bool:
    return filename in _load_suppressed()


def is_managed(path: str | Path) -> bool:
    """True if the given file was downloaded by us (and so deleting it is OK)."""
    try:
        p = Path(path).resolve()
    except Exception:
        return False
    try:
        if p.parent.resolve() != cache_logic.DATA_DIR.resolve():
            return False
    except Exception:
        return False
    return p.name in _load_marker()


# ---------- ERDDAP scan ----------

def _erddap_listing(base_url: str) -> list:
    json_url = base_url.rstrip("/") + "/.json"
    try:
        r = requests.get(json_url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        rows = r.json().get("table", {}).get("rows", []) or []
        return [{"name": row[0], "last_modified": (row[1] or 0) / 1000.0} for row in rows]
    except Exception:
        return []


def _scan_active() -> list[dict]:
    """Find recent _R.nc files across the ERDDAP server. Pure I/O, no caching."""
    out: list[dict] = []
    cutoff = time.time() - DAYS_ACTIVE * 86400
    root = _erddap_listing(SERVER_FILES_URL)
    for item in root:
        if not item["name"].endswith("/"):
            continue
        ds = item["name"].strip("/")
        if not (item["last_modified"] >= cutoff or ds.endswith("_R")):
            continue
        for f in _erddap_listing(urljoin(SERVER_FILES_URL, item["name"])):
            if not f["name"].endswith(FILE_SUFFIX):
                continue
            if f["last_modified"] < cutoff:
                continue
            out.append({
                "dataset": ds,
                "filename": f["name"],
                "url": urljoin(SERVER_FILES_URL, item["name"]) + f["name"],
                "server_mtime": f["last_modified"],
            })
    out.sort(key=lambda x: x["server_mtime"], reverse=True)
    return out


def scan_cached(force: bool = False) -> list[dict]:
    """Return the active-glider listing, sharing a result across concurrent callers."""
    now = time.time()
    with _lock:
        fresh = (now - _scan_cache["at"]) < SCAN_CACHE_TTL
        if not force and fresh and _scan_cache["data"] is not None:
            return _scan_cache["data"]
    data = _scan_active()
    with _lock:
        _scan_cache.update(at=time.time(), data=data)
    return data


# ---------- download / update ----------

def _download_to_data_dir(url: str, filename: str, server_mtime: float) -> Optional[Path]:
    target = cache_logic.DATA_DIR / filename
    cache_logic.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=HTTP_TIMEOUT * 4) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        os.replace(tmp, target)
    except Exception:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        return None

    with _lock:
        marker = _load_marker()
        marker[filename] = {"server_mtime": float(server_mtime), "downloaded_at": time.time()}
        _save_marker(marker)
    return target


def _remove_managed_file(filename: str):
    """Delete a managed file from disk + marker, and drop its cache record."""
    target = cache_logic.DATA_DIR / filename
    rid = cache_logic._file_id(target) if target.exists() else None
    if rid:
        cache_logic.remove_file(rid)
    try:
        target.unlink(missing_ok=True)
    except Exception:
        pass
    with _lock:
        marker = _load_marker()
        if filename in marker:
            marker.pop(filename, None)
            _save_marker(marker)


def _download_and_register(entry: dict):
    """Worker task: fetch the file, register it with the cache, mark as managed."""
    fname = entry["filename"]
    try:
        target = _download_to_data_dir(entry["url"], fname, entry["server_mtime"])
        if target is None:
            return
        try:
            cache_logic.register_path(str(target))
        except Exception:
            pass
    finally:
        with _lock:
            _in_flight.discard(fname)


def _enqueue_download(entry: dict):
    fname = entry["filename"]
    with _lock:
        if fname in _in_flight:
            return False
        _in_flight.add(fname)
    _executor.submit(_download_and_register, entry)
    return True


def request_download(filename: str) -> dict:
    """Public: ask to download `filename` (from the active scan) into data/."""
    listing = scan_cached(force=False)
    entry = next((e for e in listing if e["filename"] == filename), None)
    if entry is None:
        return {"status": "error", "message": "File not found in active listing"}
    _remove_suppressed(filename)   # an explicit download un-bins the glider
    started = _enqueue_download(entry)
    return {"status": "queued" if started else "in_flight", "filename": filename}


# ---------- background scanner ----------

def _scanner_loop():
    """Periodically re-scan the feed so newly-active gliders get auto-downloaded
    even while the Files panel is closed. Cheap: one ERDDAP listing per pass,
    and _maybe_auto_update's own cooldown still applies."""
    while True:
        time.sleep(SCANNER_INTERVAL)
        try:
            _maybe_auto_update(scan_cached(force=True))
        except Exception:
            pass


def _ensure_background_scanner():
    """Start the periodic scanner once (lazily, on first use of the feed)."""
    global _scanner_started
    with _lock:
        if _scanner_started:
            return
        _scanner_started = True
    threading.Thread(target=_scanner_loop, name="live-scanner", daemon=True).start()


def _maybe_auto_update(listing: list[dict]):
    """Keep the local copy in sync with the live feed (best-effort):

      * auto-download every active glider we don't already have,
      * re-download a managed file when the server has a newer copy, and
      * delete managed files once they age out of the live window.

    Gliders the user binned are skipped (suppressed).
    """
    global _last_auto_update
    now = time.time()
    with _lock:
        if (now - _last_auto_update) < AUTO_UPDATE_COOLDOWN:
            return
        _last_auto_update = now
        marker = _load_marker()
        suppressed = _load_suppressed()

    for entry in listing:
        fname = entry["filename"]
        if fname in suppressed:
            continue                       # user removed this one — leave it
        info = marker.get(fname)
        if info is None:
            _enqueue_download(entry)        # new active glider → download it
        elif entry["server_mtime"] > float(info.get("server_mtime", 0)) + 1:
            _enqueue_download(entry)        # have it, but server has a newer copy

    cutoff = now - DAYS_ACTIVE * 86400
    for fname, info in marker.items():
        if fname in _in_flight:
            continue
        if float(info.get("server_mtime", 0)) < cutoff:
            _remove_managed_file(fname)     # aged out of the live window


# ---------- public API ----------

def list_live(force_scan: bool = False) -> dict:
    """Combined live feed: server-listed active gliders + uploaded files."""
    _ensure_background_scanner()
    listing = scan_cached(force=force_scan)
    _maybe_auto_update(listing)

    marker = _load_marker()
    suppressed = _load_suppressed()
    with _lock:
        in_flight = set(_in_flight)

    # Active gliders (server-detected)
    active = []
    for e in listing:
        fname = e["filename"]
        target = cache_logic.DATA_DIR / fname
        downloaded = target.exists() and fname in marker
        local_mtime = float(marker.get(fname, {}).get("server_mtime", 0)) if downloaded else 0.0
        rid = cache_logic._file_id(target) if downloaded else None
        rec = cache_logic.get_record(rid) if rid else None
        active.append({
            "kind": "live",
            "dataset": e["dataset"],
            "filename": fname,
            "server_mtime": e["server_mtime"],
            "downloaded": downloaded,
            "managed": downloaded,
            "needs_update": downloaded and e["server_mtime"] > local_mtime + 1,
            "downloading": fname in in_flight,
            "suppressed": fname in suppressed,
            "file_id": rid if rec else None,
            "status": (rec or {}).get("status") if rec else None,
            "progress": (rec or {}).get("progress") if rec else None,
        })

    # "Your files": everything registered locally that we did NOT auto-download
    # — uploads plus any .nc the user dropped in data/ themselves.
    managed_paths = {str((cache_logic.DATA_DIR / fn).resolve()) for fn in marker.keys()}
    uploads = []
    for rec in (cache_logic.list_files() or []):
        try:
            rec_path = str(Path(rec.get("path", "")).resolve())
        except Exception:
            rec_path = rec.get("path", "")
        if rec_path in managed_paths:
            continue
        uploads.append({
            "kind": "upload" if rec.get("uploaded") else "local",
            "file_id": rec["id"],
            "name": rec["name"],
            "path": rec["path"],
            "size": rec.get("size", 0),
            "status": rec.get("status"),
            "progress": rec.get("progress"),
            "is_nrt": rec.get("is_nrt", False),
            "last_time": rec.get("last_time"),
            "uploaded": rec.get("uploaded", False),
        })

    return {
        "scanned_at": _scan_cache.get("at", 0),
        "scan_ttl": SCAN_CACHE_TTL,
        "days_active": DAYS_ACTIVE,
        "active": active,
        "uploads": uploads,
    }


def delete_managed(filename: str) -> bool:
    """Delete a managed live file (only; refuses unknown/user-placed files).
    Binning a glider also suppresses it so the auto-downloader leaves it alone
    until the user explicitly downloads it again."""
    if filename not in _load_marker():
        return False
    _add_suppressed(filename)
    _remove_managed_file(filename)
    return True
