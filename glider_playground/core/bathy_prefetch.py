"""What the three.js 3D view loads first, made once per file in the background: its seabed and its track.

The view's seabed is the 15-arc-second ETOPO 2022 for the file's 3D box (spatial_logic.fetch_bathy_grid) — a few seconds
of ERDDAP time. So the first look at a file is a disk read, one worker fetches it for every READY file (queued at the
end of cache_logic._process, and for files that predate this at startup) and keeps it in the file's binary cache
(`bathy_fine:<grid>`; cache_logic drops it when the file changes). /api/3d_bathy still fetches on demand if a file is
opened before its turn.
"""
import json
import threading
import traceback
from collections import deque

from . import cache_logic, spatial_logic

GRID = 600                      # seabed points along the box's longer side
_queue: deque = deque()
_cv = threading.Condition()
_started = False


def key(grid: int = GRID) -> str:
    return f"bathy_fine:{grid}"


def fetch(file_id: str, grid: int = GRID) -> bytes:
    """The stored seabed for a file, fetching and storing it first if need be. Raises if it can't be had."""
    hit = cache_logic.get_plot_binary(file_id, key(grid))
    if hit is not None:
        return hit
    bounds = (cache_logic.get_payload(file_id, "spatial_3d") or {}).get("bounds")
    if not bounds:
        raise LookupError("No 3D bounds for this file")
    data = json.dumps(spatial_logic.fetch_bathy_grid(bounds, grid), separators=(",", ":")).encode()
    cache_logic.put_plot_binary(file_id, key(grid), data)
    return data


TRACK_KEY = "track3d:1"


def track(file_id: str) -> bytes:
    """The file's 3D track, packed for the view: uint32 n, 4 bytes padding, float64 time_ms[n], then float32
    lon[n], lat[n], z[n], pitch[n] (NaN where missing). A third of the JSON payload's size and no JSON encoding per
    request. Made from the cached 3D payload, then kept in the file's binary cache."""
    import numpy as np
    hit = cache_logic.get_plot_binary(file_id, TRACK_KEY)
    if hit is not None:
        return hit
    p = cache_logic.get_payload(file_id, "spatial_3d")
    if not p or "lon" not in p:
        raise LookupError("No 3D track for this file")
    n = len(p["lon"])
    col = lambda name, kind: np.array([np.nan if v is None else v for v in (p.get(name) or [None] * n)], dtype=kind)  # noqa: E731
    data = b"".join([np.array([n, 0], dtype="<u4").tobytes(), col("time_ms", "<f8").tobytes(),
                     *(col(name, "<f4").tobytes() for name in ("lon", "lat", "elevation", "pitch"))])
    cache_logic.put_plot_binary(file_id, TRACK_KEY, data)
    return data


def ensure(file_id: str) -> None:
    with _cv:
        if file_id not in _queue:
            _queue.append(file_id)
            _cv.notify()


def _worker() -> None:
    while True:
        with _cv:
            while not _queue:
                _cv.wait()
            file_id = _queue.popleft()
        try:
            rec = cache_logic.get_record(file_id)
            if rec and rec.get("status") == cache_logic.STATUS_READY:
                track(file_id)
                fetch(file_id)
        except Exception:  # noqa: BLE001 — best effort: the route fetches on demand
            traceback.print_exc()


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, name="bathy-prefetch", daemon=True).start()
    for rec in cache_logic.list_files():
        if rec.get("status") == cache_logic.STATUS_READY:
            ensure(rec["id"])
