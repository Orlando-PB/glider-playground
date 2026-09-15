"""Per-file Copernicus overlay prefetch + on-disk store.

Every registered file gets all of its surface overlays (the OVERLAYS scalars
plus the currents grid) fetched once, in the background, as the last stage of
processing — so by the time a user opens the Layers panel the fields are already
on disk and a click is a local read rather than a ~3s Copernicus round-trip.

Storage reuses cache_logic's per-file binary cache (get/put_plot_binary): the
key folds in the file signature + CACHE_VERSION, so a changed or re-downloaded
file, a version bump, or a delete invalidates the stored fields for free. A
finished deployment's overlay is a fixed point in time and is fetched exactly
once in its lifetime. A live glider (last fix inside the live window) uses the
most recent available field; a refresh loop re-checks those against the
dataset's latest day and only re-downloads a layer when a newer day exists.

One worker thread serialises the fetches (kind to the Pi and to Copernicus);
files the user is actually looking at jump the queue via ensure(front=True).
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import time
from collections import deque
from datetime import datetime, timezone

from . import cache_logic
from . import overlay_logic
from . import server_config
from . import spatial_logic

logger = logging.getLogger(__name__)

LAYERS: list[str] = list(overlay_logic.OVERLAYS.keys()) + ["currents"]

# A glider whose last fix is within this many days is treated as "live": its
# overlay uses the most recent available Copernicus field rather than the exact
# last-fix date, so an active deployment always sees the freshest ocean state.
LIVE_WINDOW_DAYS = 7

REFRESH_INTERVAL = 300.0          # live-file staleness check cadence (s)
_LAYER_PAUSE_SERVER = 0.5         # breather between layers on the Pi

_lock = threading.Lock()
_queue: deque[str] = deque()
_cv = threading.Condition(_lock)
_started = False
# file_id -> {"layers": {key: {"state": pending|ready|error, "date": str|None,
#             "error": str|None, "setup": str|None}}, "live": bool, "done": bool}
_status: dict[str, dict] = {}
# Distinct failure messages already reported: without credentials every layer
# of every file fails the same way, so warn once per message, not 8×N times.
_warned: set[str] = set()


# ---------- date rule ----------

def target_date(rec) -> str | None:
    """Overlay date for a file: the glider's last data point for a past
    deployment, or None (→ most recent available) when the glider is still live."""
    if not rec or not rec.get("last_time"):
        return None
    last_str = str(rec["last_time"])[:10]
    try:
        last = datetime.strptime(last_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return last_str
    age_days = (datetime.now(timezone.utc) - last).days
    return None if age_days <= LIVE_WINDOW_DAYS else last_str


def is_live(rec) -> bool:
    return bool(rec) and target_date(rec) is None


# ---------- store ----------

def _store_key(key: str) -> str:
    return f"overlay:{key}"


def get_layer_bytes(file_id: str, key: str) -> bytes | None:
    """Stored payload for a layer (packed binary for scalars, JSON for
    currents), or None if not fetched yet / file not ready."""
    return cache_logic.get_plot_binary(file_id, _store_key(key))


def _stored_date(data: bytes, key: str) -> str | None:
    try:
        if key == "currents":
            return json.loads(data.decode("utf-8")).get("date")
        (hlen,) = struct.unpack("<I", data[:4])
        return json.loads(data[4:4 + hlen].decode("utf-8")).get("date")
    except Exception:
        return None


def _location(file_id: str) -> dict | None:
    loc = cache_logic.get_payload(file_id, "location")
    if loc is None:
        path = cache_logic.resolve_path(file_id)
        if not path:
            return None
        try:
            loc = spatial_logic.get_location_summary(path)
        except Exception:
            return None
    if not loc or "error" in loc:
        return None
    return loc


def fetch_layer(file_id: str, key: str) -> tuple[bytes | None, dict | None]:
    """Fetch one layer for a file from Copernicus and persist it. Returns
    (bytes, None) on success or (None, error_dict) on failure. Shared by the
    background worker and the on-demand API fallback."""
    loc = _location(file_id)
    if loc is None:
        return None, {"error": "No spatial data for this file", "hint": ""}
    rec = cache_logic.get_record(file_id)
    date = target_date(rec)
    bbox = dict(lat_min=loc["lat_min"], lat_max=loc["lat_max"],
                lon_min=loc["lon_min"], lon_max=loc["lon_max"])
    if key == "currents":
        result = overlay_logic.fetch_currents(target_date=date, **bbox)
    else:
        result = overlay_logic.fetch_overlay(key, target_date=date, **bbox)
    if "error" in result:
        return None, result
    result.pop("_timing", None)
    if key == "currents":
        data = json.dumps(result).encode("utf-8")
    else:
        data = overlay_logic.pack_overlay_response(result)
    cache_logic.put_plot_binary(file_id, _store_key(key), data)
    # An on-demand fetch (user clicked before the worker reached this layer)
    # is just as final as a prefetched one — reflect it in the status so the
    # button un-greys on the next poll instead of waiting for the worker.
    with _lock:
        st = _status.get(file_id)
        if st is not None:
            st["layers"][key].update(state="ready", date=_stored_date(data, key), error=None, setup=None)
            st["done"] = all(l["state"] != "pending" for l in st["layers"].values())
    return data, None


# ---------- status ----------

def _blank(rec) -> dict:
    return {
        "layers": {k: {"state": "pending", "date": None, "error": None, "setup": None}
                   for k in LAYERS},
        "live": is_live(rec),
        "done": False,
    }


def _sync_from_disk(file_id: str, st: dict) -> None:
    """Mark layers already on disk as ready (survives restarts: the store is
    the source of truth, the status dict is just a cache of it)."""
    for k, layer in st["layers"].items():
        if layer["state"] == "ready":
            continue
        data = get_layer_bytes(file_id, k)
        if data is not None:
            layer.update(state="ready", date=_stored_date(data, k), error=None, setup=None)
    st["done"] = all(l["state"] != "pending" for l in st["layers"].values())


def get_status(file_id: str) -> dict:
    """Current per-layer state for a file, kicking off the prefetch (at the
    front of the queue — the user is looking at this one) if needed."""
    rec = cache_logic.get_record(file_id)
    if not rec or rec.get("status") != cache_logic.STATUS_READY:
        return {"layers": {k: {"state": "pending"} for k in LAYERS},
                "live": is_live(rec), "done": False, "file_ready": False}
    ensure(file_id, front=True)
    with _lock:
        st = _status.get(file_id) or _blank(rec)
        out = {"layers": {k: dict(v) for k, v in st["layers"].items()},
               "live": st["live"], "done": st["done"], "file_ready": True}
    return out


def ensure(file_id: str, front: bool = False) -> None:
    """Queue a file for prefetch unless it's already complete or queued."""
    rec = cache_logic.get_record(file_id)
    if not rec or rec.get("status") != cache_logic.STATUS_READY:
        return
    with _cv:
        st = _status.get(file_id)
        if st is None:
            st = _blank(rec)
            _sync_from_disk(file_id, st)
            _status[file_id] = st
        if st["done"]:
            return
        if file_id in _queue:
            if front:
                _queue.remove(file_id)
                _queue.appendleft(file_id)
            return
        (_queue.appendleft if front else _queue.append)(file_id)
        _cv.notify()


def ensure_all() -> None:
    for rec in cache_logic.list_files():
        if rec.get("status") == cache_logic.STATUS_READY:
            ensure(rec["id"])


def forget(file_id: str) -> None:
    """Drop in-memory state for a file whose stored layers were invalidated
    (reprocess / delete). Called from cache_logic.clear_plot_binary."""
    with _cv:
        _status.pop(file_id, None)
        try:
            _queue.remove(file_id)
        except ValueError:
            pass


def retry_errors() -> None:
    """Re-queue every errored layer (after a successful Copernicus login)."""
    with _cv:
        for fid, st in _status.items():
            changed = False
            for layer in st["layers"].values():
                if layer["state"] == "error":
                    layer.update(state="pending", error=None, setup=None)
                    changed = True
            if changed:
                st["done"] = False
                if fid not in _queue:
                    _queue.append(fid)
        _cv.notify()


# ---------- worker ----------

def _run_file(file_id: str) -> None:
    with _lock:
        st = _status.get(file_id)
        if st is None:
            return
        pending = [k for k, l in st["layers"].items() if l["state"] == "pending"]
    for key in pending:
        rec = cache_logic.get_record(file_id)
        if not rec or rec.get("status") != cache_logic.STATUS_READY:
            return   # file went away / is reprocessing — forget() handles state
        t0 = time.time()
        try:
            data, err = fetch_layer(file_id, key)
        except Exception as exc:
            data, err = None, {"error": f"{type(exc).__name__}: {exc}", "hint": ""}
        with _lock:
            st = _status.get(file_id)
            if st is None:
                return
            layer = st["layers"][key]
            if data is not None:
                layer.update(state="ready", date=_stored_date(data, key), error=None, setup=None)
                logger.debug("Prefetched %s for %s in %.1fs", key, rec.get("name", file_id), time.time() - t0)
            else:
                layer.update(state="error", error=err.get("error"), setup=err.get("setup"))
                msg = str(err.get("error"))
                if msg not in _warned:
                    _warned.add(msg)
                    logger.warning("Overlay prefetch failed (%s, %s): %s — further identical "
                                   "failures are logged at DEBUG", key, rec.get("name", file_id), msg)
                else:
                    logger.debug("Prefetch %s for %s failed: %s", key, rec.get("name", file_id), msg)
            st["done"] = all(l["state"] != "pending" for l in st["layers"].values())
        if server_config.IS_SERVER:
            time.sleep(_LAYER_PAUSE_SERVER)


def _worker_loop() -> None:
    while True:
        with _cv:
            while not _queue:
                _cv.wait()
            file_id = _queue.popleft()
        try:
            _run_file(file_id)
        except Exception:
            logger.exception("Overlay prefetch failed for %s", file_id)


def _refresh_loop() -> None:
    """Every REFRESH_INTERVAL, re-check live files' stored layers against the
    dataset's latest available day and re-queue only the layers that are stale.
    Nothing is re-downloaded when Copernicus hasn't published a newer day."""
    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            _refresh_live()
        except Exception:
            logger.exception("Overlay refresh pass failed")


def _refresh_live() -> None:
    with _lock:
        live_ids = [fid for fid, st in _status.items() if st["live"] and st["done"]]
    if not live_ids:
        return
    latest = {k: overlay_logic.latest_available_date(k) for k in LAYERS}
    # Look records up before taking our lock: cache_logic calls forget() while
    # holding its own lock, so never nest theirs inside ours.
    recs = {fid: cache_logic.get_record(fid) for fid in live_ids}
    with _cv:
        for fid in live_ids:
            st = _status.get(fid)
            rec = recs.get(fid)
            if st is None or not rec:
                continue
            if not is_live(rec):
                st["live"] = False   # aged out of the live window: now fixed
                continue
            stale = False
            for k, layer in st["layers"].items():
                if layer["state"] != "ready":
                    continue
                new = latest.get(k)
                if new and layer["date"] and new > layer["date"]:
                    layer.update(state="pending")
                    stale = True
            if stale:
                st["done"] = False
                if fid not in _queue:
                    _queue.append(fid)
                logger.debug("Live overlays for %s stale — refreshing", rec.get("name", fid))
        _cv.notify()


def start() -> None:
    """Start the worker + live-refresh threads and queue every ready file.
    Idempotent; safe to call from app import time."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_worker_loop, name="overlay-prefetch", daemon=True).start()
    threading.Thread(target=_refresh_loop, name="overlay-refresh", daemon=True).start()
    threading.Thread(target=ensure_all, name="overlay-prefetch-seed", daemon=True).start()
