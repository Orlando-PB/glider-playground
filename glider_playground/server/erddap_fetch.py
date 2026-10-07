"""Live glider feed.

Scans an ERDDAP file index for files updated within the past `DAYS_ACTIVE`
days, downloads them into `DATA_DIR`, and tracks ownership in a marker file
so that deletes only ever touch files we wrote — never user-placed data.

Designed to run on a small server (Raspberry Pi) shared between users:
  * One background thread (`start`, at app startup) scans ERDDAP every
    `SCAN_INTERVAL` and then downloads / updates / prunes managed files. Requests
    never start a scan: `list_live` answers from the last result, and the
    Refresh button only wakes the scanner early.
  * Downloads are serialised on a single background worker.
  * Files the server hasn't updated for `PRUNE_DAYS` are pruned automatically.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests

from ..core import cache_logic

SERVER_FILES_URL = "https://linkedsystems.uk/erddap/files/"
SERVER_INFO_URL = "https://linkedsystems.uk/erddap/info/"
DAYS_ACTIVE = 7
PRUNE_DAYS = 30               # a managed file not updated on the server for this long is deleted
FILE_SUFFIX = "_R.nc"
SCAN_INTERVAL = 900           # seconds between scans (each is ~80 folder listings on BODC)
SCAN_RETRY = 120              # seconds before retrying a scan BODC didn't answer
HTTP_TIMEOUT = 15
LISTING_TIMEOUT = 6           # seconds per directory-listing attempt
LISTING_ATTEMPTS = 3

MARKER_FILE = cache_logic.DATA_DIR / ".glider_playground_managed.json"
# Gliders the user "binned": never auto-download these again until they ask
# for one explicitly (a manual download clears the suppression).
SUPPRESS_FILE = cache_logic.DATA_DIR / ".glider_playground_suppressed.json"

log = logging.getLogger(__name__)

_lock = threading.RLock()
_scan_cache: dict = {"at": 0.0, "data": None, "scanning": True, "error": False}
_wake = threading.Event()     # set by the Refresh button to scan now
_in_flight: set[str] = set()  # filenames currently downloading
# filename -> {"error", "server_mtime", "gone"} for its last failed download (shown on its card until retried).
# "gone" (a 404) skips auto-download until the server lists a newer copy.
_failed: dict[str, dict] = {}
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

def _erddap_listing(base_url: str) -> Optional[list]:
    """Directory rows, or None when the request failed (distinct from empty)."""
    json_url = base_url.rstrip("/") + "/.json"
    # BODC normally answers in <1s but occasionally stalls a request outright,
    # so fail fast and retry rather than wait on one long timeout.
    for attempt in range(LISTING_ATTEMPTS):
        try:
            r = requests.get(json_url, timeout=LISTING_TIMEOUT)
            r.raise_for_status()
            rows = r.json().get("table", {}).get("rows", []) or []
            return [{"name": row[0], "last_modified": (row[1] or 0) / 1000.0, "size": row[2]} for row in rows]
        except Exception as e:
            log.warning("ERDDAP listing failed (%d/%d) %s: %s", attempt + 1, LISTING_ATTEMPTS, json_url, e)
            time.sleep(0.5)
    return None


def _scan_active(previous: Optional[list] = None) -> Optional[list[dict]]:
    """Find recent _R.nc files across the ERDDAP server. None if the server is
    unreachable; a single failed folder keeps its entries from `previous`
    instead of silently dropping that glider."""
    cutoff = time.time() - DAYS_ACTIVE * 86400
    root = _erddap_listing(SERVER_FILES_URL)
    if root is None:
        return None
    folders = [
        item["name"] for item in root
        if item["name"].endswith("/")
        and (item["last_modified"] >= cutoff or item["name"].strip("/").endswith("_R"))
    ]
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="live-scan") as pool:
        listings = list(pool.map(lambda n: _erddap_listing(urljoin(SERVER_FILES_URL, n)), folders))

    out: list[dict] = []
    for name, files in zip(folders, listings):
        ds = name.strip("/")
        if files is None:
            out.extend(e for e in (previous or [])
                       if e["dataset"] == ds and e["server_mtime"] >= cutoff)
            continue
        for f in files:
            if not f["name"].endswith(FILE_SUFFIX) or f["last_modified"] < cutoff:
                continue
            out.append({
                "dataset": ds,
                "filename": f["name"],
                "url": urljoin(SERVER_FILES_URL, name) + f["name"],
                "server_mtime": f["last_modified"],
                "size": f["size"],
            })
    out.sort(key=lambda x: x["server_mtime"], reverse=True)
    return out


# ---------- download / update ----------

def _download_to_data_dir(url: str, filename: str, server_mtime: float) -> Path:
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
        tmp.unlink(missing_ok=True)
        raise

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


DATASET_FIELDS = ("title", "Conventions", "processing_level", "time_coverage_start", "time_coverage_end", "date_modified")


def _dataset_info(dataset: str) -> dict:
    """The dataset's global attributes from ERDDAP that help a data manager place a failed file, or {}."""
    try:
        r = requests.get(f"{SERVER_INFO_URL}{dataset}/index.json", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        rows = r.json()["table"]["rows"]
    except Exception:
        return {}
    # rows are [row type, variable, attribute, data type, value]
    return {row[2]: row[4] for row in rows if row[1] == "NC_GLOBAL" and row[2] in DATASET_FIELDS}


def _failure_details(entry: dict, error: Exception) -> dict:
    """What we know about a file BODC lists but we couldn't download, for the card's tooltip."""
    details = {"url": entry["url"], "size": entry.get("size"), "server_mtime": entry["server_mtime"]}
    response = getattr(error, "response", None)
    if response is not None:
        details["http_status"] = response.status_code
        details["answered_by"] = response.headers.get("Server", "")
    details["dataset"] = _dataset_info(entry["dataset"])
    return details


def _download_and_register(entry: dict):
    """Worker task: fetch the file, register it with the cache, mark as managed."""
    fname = entry["filename"]
    try:
        target = _download_to_data_dir(entry["url"], fname, entry["server_mtime"])
        cache_logic.register_path(str(target))
    except Exception as e:
        gone = isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 404
        if gone:
            log.warning("%s is listed on BODC but not served (404); skipping until BODC updates it.", fname)
        else:
            log.warning("Live download failed for %s: %s", fname, e)
        details = _failure_details(entry, e)
        with _lock:
            _failed[fname] = {"error": str(e), "server_mtime": entry["server_mtime"], "gone": gone,
                              "details": details}
    finally:
        with _lock:
            _in_flight.discard(fname)


def _enqueue_download(entry: dict):
    fname = entry["filename"]
    with _lock:
        if fname in _in_flight:
            return False
        _in_flight.add(fname)
        _failed.pop(fname, None)
    _executor.submit(_download_and_register, entry)
    return True


def request_download(filename: str) -> dict:
    """Public: ask to download `filename` (from the active scan) into data/."""
    with _lock:
        listing = _scan_cache["data"] or []
    entry = next((e for e in listing if e["filename"] == filename), None)
    if entry is None:
        return {"status": "error", "message": "File not found in active listing"}
    _remove_suppressed(filename)   # an explicit download un-bins the glider
    started = _enqueue_download(entry)
    return {"status": "queued" if started else "in_flight", "filename": filename}


# ---------- background scanner ----------

def _scan_once():
    with _lock:
        previous = _scan_cache["data"]
        _scan_cache["scanning"] = True
    listing = None
    try:
        listing = _scan_active(previous)
    except Exception:
        log.exception("Live scan failed")
    finally:
        with _lock:
            _scan_cache["scanning"] = False
            _scan_cache["error"] = listing is None
            if listing is not None:
                _scan_cache.update(at=time.time(), data=listing)
    if listing is not None:
        try:
            _sync_managed_files(listing)
        except Exception:
            log.exception("Live sync failed")


def _scanner_loop():
    while True:
        _wake.clear()
        _scan_once()
        with _lock:
            failed = _scan_cache["error"]
        _wake.wait(SCAN_RETRY if failed else SCAN_INTERVAL)


def start():
    """Start the background scanner (once). Called at app startup."""
    global _scanner_started
    with _lock:
        if _scanner_started:
            return
        _scanner_started = True
    threading.Thread(target=_scanner_loop, name="live-scanner", daemon=True).start()


def _sync_managed_files(listing: list[dict]):
    """Keep the local copy in sync with the live feed (best-effort):

      * auto-download every active glider we don't already have,
      * re-download a managed file when the server has a newer copy, and
      * delete managed files the server has not updated for PRUNE_DAYS.

    Gliders the user binned are skipped (suppressed), and so are files the server
    lists but answered 404 for, until it lists a newer copy.
    """
    now = time.time()
    with _lock:
        marker = _load_marker()
        suppressed = _load_suppressed()
        failed_now = dict(_failed)

    for entry in listing:
        fname = entry["filename"]
        if fname in suppressed:
            continue                       # user removed this one — leave it
        failed = failed_now.get(fname)
        if failed and failed["gone"] and failed["server_mtime"] == entry["server_mtime"]:
            continue                       # listed but not served (404) — wait for a newer copy
        info = marker.get(fname)
        if info is None:
            _enqueue_download(entry)        # new active glider → download it
        elif entry["server_mtime"] > float(info.get("server_mtime", 0)) + 1:
            _enqueue_download(entry)        # have it, but server has a newer copy

    cutoff = now - PRUNE_DAYS * 86400
    for fname, info in marker.items():
        if fname in _in_flight:
            continue
        if float(info.get("server_mtime", 0)) < cutoff:
            _remove_managed_file(fname)     # not updated for PRUNE_DAYS


# ---------- public API ----------

def list_live(force_scan: bool = False) -> dict:
    """Combined live feed: the last scan's active gliders + uploaded files. Never waits on ERDDAP."""
    if force_scan:
        _wake.set()
    with _lock:
        listing = _scan_cache["data"] or []

    marker = _load_marker()
    suppressed = _load_suppressed()
    with _lock:
        in_flight = set(_in_flight)
        failed = dict(_failed)

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
            "download_error": (failed.get(fname) or {}).get("error"),
            "download_details": (failed.get(fname) or {}).get("details"),
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
        "scanning": _scan_cache["scanning"] or _wake.is_set(),
        "scan_error": _scan_cache["error"],
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
