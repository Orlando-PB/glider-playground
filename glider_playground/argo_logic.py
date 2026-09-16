"""Argo float explorer (experimental, self-contained).

Everything Argo lives here + ``static/argo_layer.js``; the rest of the app only
touches it through two ``/api/argo/*`` routes in ``app.py`` and three one-line
hooks in ``map_view.html``. Delete those and this file and the playground is
back to gliders-only.

Two upstream sources, both read-only:

* The Argo GDAC global profile index (``ar_index_global_prof.txt.gz``, ~60 MB
  gzipped, one row per profile, ~3.4M rows). Downloaded at most once per
  ``INDEX_TTL`` into ``~/.glider_playground/argo/`` and reduced in a background
  thread to one record per float (last position/date, first date, profile
  count). The reduced table is persisted as ``floats.json`` so a restart
  serves instantly.
* Euro-Argo fleet monitoring (``fleetmonitoring.euro-argo.eu/floats/<wmo>``)
  for the per-float detail card: platform/deployment/PI/last cycle etc. Proxied
  and trimmed here so the browser never talks to a third-party host directly;
  cached in-process for ``DETAIL_TTL``.

Nothing here downloads profile data or touches the file cache.
"""

from __future__ import annotations

import array
import gzip
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import requests

from . import cache_logic

log = logging.getLogger(__name__)

ARGO_DIR = cache_logic.CACHE_ROOT / "argo"
INDEX_URL = "https://data-argo.ifremer.fr/ar_index_global_prof.txt.gz"
INDEX_GZ = ARGO_DIR / "ar_index_global_prof.txt.gz"
FLOATS_JSON = ARGO_DIR / "floats.json"
# Per-profile arrays (every positioned profile in the index), written next to
# floats.json by the same build pass so the 3D view can ask "which floats
# surfaced inside this box during this deployment?" without rescanning the
# 60 MB index. Row-aligned float32 lat/lon, int64 date (YYYYMMDDHHMMSS) and
# int32 WMO; ~60 MB for the ~3M-row global index, mmapped on query.
PROFILES_NPZ_STEM = ARGO_DIR / "profiles"
_PROFILE_ARRAYS = ("lat", "lon", "date", "wmo")
DETAIL_URL = "https://fleetmonitoring.euro-argo.eu/floats/{wmo}"

INDEX_TTL = 24 * 3600        # re-download the global index at most daily
DETAIL_TTL = 3600            # per-float detail cache
HTTP_TIMEOUT = 30
INDEX_TIMEOUT = 600          # the index is ~60 MB; slow on a Pi

_lock = threading.Lock()
_floats: Optional[dict] = None       # {"built": epoch, "floats": [...]}
_building = False
_build_error: Optional[str] = None
_detail_cache: dict[str, tuple[float, dict]] = {}


# ---------- global index → one record per float ----------

def _parse_index(path: Path) -> list[dict]:
    """Reduce the profile index to one record per float.

    Rows: file,date,latitude,longitude,ocean,profiler_type,institution,date_update
    ``file`` is ``<dac>/<wmo>/profiles/<R|D><wmo>_<cycle>[D].nc``. Dates are
    ``YYYYMMDDHHMMSS`` strings, so they compare lexically.
    """
    floats: dict[str, dict] = {}
    p_lat, p_lon, p_date, p_wmo = array.array("f"), array.array("f"), array.array("q"), array.array("i")
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line or line[0] == "#" or line.startswith("file,"):
                continue
            p = line.rstrip("\n").split(",")
            if len(p) < 4:
                continue
            fparts = p[0].split("/")
            if len(fparts) < 2:
                continue
            dac, wmo = fparts[0], fparts[1]
            date = p[1]
            rec = floats.get(wmo)
            if rec is None:
                rec = floats[wmo] = {
                    "wmo": wmo, "dac": dac, "inst": p[6] if len(p) > 6 else "",
                    "ocean": p[4] if len(p) > 4 else "",
                    "first": date, "last": "", "lat": None, "lon": None, "n": 0,
                }
            rec["n"] += 1
            if date and date < rec["first"]:
                rec["first"] = date
            # Last *positioned* profile wins for lat/lon; last date regardless.
            if date and date > rec["last"]:
                rec["last"] = date
                try:
                    lat, lon = float(p[2]), float(p[3])
                    if -90 <= lat <= 90 and -180 <= lon <= 360:
                        rec["lat"], rec["lon"] = round(lat, 4), round(lon, 4)
                except ValueError:
                    pass
            # Every positioned, dated profile goes into the per-profile arrays.
            if date and len(date) >= 8 and date.isdigit():
                try:
                    lat, lon = float(p[2]), float(p[3])
                except ValueError:
                    continue
                if -90 <= lat <= 90 and -180 <= lon <= 360 and wmo.isdigit():
                    if lon > 180:
                        lon -= 360
                    p_lat.append(lat); p_lon.append(lon)
                    p_date.append(int(date.ljust(14, "0")[:14])); p_wmo.append(int(wmo))
    _write_profiles(p_lat, p_lon, p_date, p_wmo)
    out = [r for r in floats.values() if r["lat"] is not None and r["last"]]
    out.sort(key=lambda r: r["last"], reverse=True)
    return out


def _write_profiles(p_lat, p_lon, p_date, p_wmo):
    """Persist the per-profile arrays as separate .npy files (mmap-able)."""
    try:
        ARGO_DIR.mkdir(parents=True, exist_ok=True)
        for name, arr, dt in (("lat", p_lat, np.float32), ("lon", p_lon, np.float32),
                              ("date", p_date, np.int64), ("wmo", p_wmo, np.int32)):
            f = Path(f"{PROFILES_NPZ_STEM}_{name}.npy")
            tmp = f.with_suffix(".tmp.npy")
            np.save(str(tmp), np.frombuffer(arr, dtype=arr.typecode).astype(dt, copy=False))
            tmp.replace(f)
        with _lock:
            _profiles_cache.clear()
    except Exception as e:  # noqa: BLE001
        log.warning("argo: could not write profile arrays: %s", e)


# ---------- profiles inside a box + time window (3D view) ----------

_profiles_cache: dict[tuple, dict] = {}
PROFILES_MAX = 2000


def _date_int(ms: float) -> int:
    d = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return int(d.strftime("%Y%m%d%H%M%S"))


def profiles_in(min_lat: float, max_lat: float, min_lon: float, max_lon: float,
                t0_ms: float, t1_ms: float) -> dict:
    """Positioned Argo profiles inside the box whose date lies in [t0, t1].

    Returns ``{"status": "ready"|"building"|"error", "count": n,
    "profiles": [[wmo, lat, lon, epoch_ms], ...]}`` sorted by time. Serves
    from the mmapped per-profile arrays; kicks the index build if missing.
    """
    _ensure_loaded()
    files = {n: Path(f"{PROFILES_NPZ_STEM}_{n}.npy") for n in _PROFILE_ARRAYS}
    if not all(f.exists() for f in files.values()):
        with _lock:
            err = _build_error if not _building else None
        return {"status": "error" if err else "building", "error": err, "count": 0, "profiles": []}
    key = (round(min_lat, 3), round(max_lat, 3), round(min_lon, 3), round(max_lon, 3),
           int(t0_ms // 3600000), int(t1_ms // 3600000), files["lat"].stat().st_mtime_ns)
    with _lock:
        hit = _profiles_cache.get(key)
    if hit is not None:
        return hit
    try:
        lat = np.load(str(files["lat"]), mmap_mode="r")
        lon = np.load(str(files["lon"]), mmap_mode="r")
        m = (lat >= min_lat) & (lat <= max_lat) & (lon >= min_lon) & (lon <= max_lon)
        idx = np.flatnonzero(m)
        rows = []
        if idx.size:
            date = np.load(str(files["date"]), mmap_mode="r")[idx]
            d0, d1 = _date_int(t0_ms), _date_int(t1_ms)
            sel = (date >= d0) & (date <= d1)
            idx, date = idx[sel], date[sel]
            wmo = np.load(str(files["wmo"]), mmap_mode="r")[idx]
            order = np.argsort(date, kind="stable")
            for j in order[:PROFILES_MAX]:
                ds = str(int(date[j])).rjust(14, "0")
                try:
                    ms = datetime.strptime(ds, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp() * 1000.0
                except ValueError:
                    continue
                rows.append([int(wmo[j]), round(float(lat[idx[j]]), 4), round(float(lon[idx[j]]), 4), ms])
        out = {"status": "ready", "count": len(rows), "profiles": rows}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": str(e), "count": 0, "profiles": []}
    with _lock:
        if len(_profiles_cache) > 64:
            _profiles_cache.clear()
        _profiles_cache[key] = out
    return out


def _download_index() -> bool:
    ARGO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_GZ.with_suffix(".part")
    try:
        with requests.get(INDEX_URL, stream=True, timeout=INDEX_TIMEOUT) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        tmp.replace(INDEX_GZ)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("argo: index download failed: %s", e)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _build_worker():
    global _floats, _building, _build_error
    try:
        fresh = INDEX_GZ.exists() and (time.time() - INDEX_GZ.stat().st_mtime) < INDEX_TTL
        if not fresh and not _download_index() and not INDEX_GZ.exists():
            raise RuntimeError("could not download the Argo index")
        t0 = time.time()
        recs = _parse_index(INDEX_GZ)
        data = {"built": time.time(), "floats": recs}
        ARGO_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FLOATS_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        tmp.replace(FLOATS_JSON)
        with _lock:
            _floats = data
            _build_error = None
        log.info("argo: index reduced to %d floats in %.1fs", len(recs), time.time() - t0)
    except Exception as e:  # noqa: BLE001
        log.warning("argo: build failed: %s", e)
        with _lock:
            _build_error = str(e)
    finally:
        with _lock:
            _building = False


def _ensure_loaded():
    """Load the persisted table if we have one; kick a rebuild if it's stale/missing."""
    global _floats, _building
    with _lock:
        if _floats is None and FLOATS_JSON.exists():
            try:
                _floats = json.loads(FLOATS_JSON.read_text())
            except Exception:  # noqa: BLE001
                _floats = None
        stale = _floats is None or (time.time() - _floats.get("built", 0)) > INDEX_TTL
        if stale and not _building:
            _building = True
            threading.Thread(target=_build_worker, name="argo-index", daemon=True).start()


def list_floats(days: Optional[float] = None) -> dict:
    """Floats whose last profile is within ``days`` (None/0 = all).

    Returns ``{"status": "ready"|"building"|"error", "built": iso, "count": n,
    "floats": [[wmo, lat, lon, last_iso, n_profiles, first_iso, dac], ...]}``
    — compact row arrays; the whole fleet is ~20k rows.
    """
    _ensure_loaded()
    with _lock:
        data = _floats
        building = _building
        err = _build_error
    if data is None:
        return {"status": "error" if err and not building else "building",
                "error": err, "count": 0, "floats": []}
    cutoff = ""
    if days:
        cutoff = datetime.fromtimestamp(time.time() - days * 86400, tz=timezone.utc).strftime("%Y%m%d%H%M%S")
    rows = []
    for r in data["floats"]:
        if cutoff and r["last"] < cutoff:
            break   # sorted newest-first
        rows.append([r["wmo"], r["lat"], r["lon"], _iso(r["last"]), r["n"], _iso(r["first"]), r["dac"]])
    return {
        "status": "ready",
        "refreshing": building,
        "built": datetime.fromtimestamp(data["built"], tz=timezone.utc).isoformat(timespec="seconds"),
        "count": len(rows),
        "floats": rows,
    }


def _iso(d: str) -> str:
    """'YYYYMMDDHHMMSS' → 'YYYY-MM-DDTHH:MM:SSZ' (naive-UTC convention + explicit Z)."""
    if not d or len(d) < 8:
        return ""
    d = d.ljust(14, "0")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{d[8:10]}:{d[10:12]}:{d[12:14]}Z"


# ---------- per-float detail (Euro-Argo fleet monitoring) ----------

def _measure(m: Optional[dict]) -> Optional[dict]:
    if not m:
        return None
    return {"pres": m.get("pres"), "temp": m.get("temp"), "psal": m.get("psal")}


def float_detail(wmo: str) -> dict:
    wmo = "".join(ch for ch in str(wmo) if ch.isdigit())
    if not wmo:
        return {"error": "bad wmo"}
    now = time.time()
    hit = _detail_cache.get(wmo)
    if hit and now - hit[0] < DETAIL_TTL:
        return hit[1]
    try:
        r = requests.get(DETAIL_URL.format(wmo=wmo), timeout=HTTP_TIMEOUT,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        d = r.json()
    except ValueError:
        return {"wmo": wmo, "error": "No record for this float in Euro-Argo fleet monitoring"}
    except Exception as e:  # noqa: BLE001
        return {"wmo": wmo, "error": f"fleet monitoring unavailable: {e}"}

    dep = d.get("deployment") or {}
    last = d.get("lastCycleBasicInfo") or {}
    plat = d.get("platform") or {}
    dc = d.get("dataCenter") or {}
    locs = []
    for L in d.get("locations") or []:
        try:
            if L.get("lat") is None or L.get("lon") is None:
                continue
            locs.append([round(float(L["lat"]), 4), round(float(L["lon"]), 4), (L.get("date") or "")[:19], L.get("cycleNumber")])
        except (TypeError, ValueError):
            continue
    out = {
        "wmo": wmo,
        "status": {"A": "Active", "I": "Inactive", "C": "Closed", "D": "Dead"}.get(d.get("statusCode"), d.get("statusCode")),
        "maker": d.get("maker"),
        "platform_type": plat.get("type"),
        "model": d.get("model"),
        "transmission": d.get("transmissionSystem"),
        "owner": d.get("owner"),
        "data_centre": dc.get("name") or dc.get("code"),
        "projects": d.get("projects") or ([d.get("projectName")] if d.get("projectName") else []),
        "networks": d.get("networks") or [],
        "sensors": [s.get("id") for s in (d.get("sensors") or []) if s.get("id")],
        "grey_list": d.get("greyListParameters") or [],
        "deployment": {
            "date": (dep.get("launchDate") or "")[:19],
            "lat": dep.get("lat"), "lon": dep.get("lon"),
            "ship": dep.get("platform"), "cruise": dep.get("cruiseName"),
            "pi": dep.get("principalInvestigatorName"),
        },
        "last_cycle": {
            "cycle": last.get("numCycle"),
            "date": (last.get("date") or "")[:19],
            "lat": last.get("lat"), "lon": last.get("lon"),
            "surface": _measure(last.get("surfaceMeasure")),
            "bottom": _measure(last.get("bottomMeasure")),
        },
        "n_cycles": len(d.get("cycles") or []),
        "locations": locs,
        "link": f"https://fleetmonitoring.euro-argo.eu/float/{wmo}",
    }
    _detail_cache[wmo] = (now, out)
    return out
