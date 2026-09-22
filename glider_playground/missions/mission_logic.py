"""Mission files -> what the mission view needs: resolved platforms, merged bathymetry, float and platform tracks.

A mission is one JSON file (schema: README.md, example: examples/biocarbon.json). Bundled examples live in
./examples; user missions in <cache root>/missions/*.json. Platforms reference registered .nc files by
filename, so a mission is "ready" once those files are in the app. Bundles (.zip = mission.json + data/*.nc)
move a whole mission between machines; see bundle.py.
"""
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..core import cache_logic, spatial_logic
from ..maps import argo_logic
from ..server import server_config

from . import live_logic

logger = logging.getLogger(__name__)

EXAMPLES_DIR = Path(__file__).parent / "examples"
MISSIONS_DIR = Path(cache_logic.CACHE_ROOT) / "missions"
DATA_DIR = MISSIONS_DIR / "data"          # .nc files unpacked from bundles
SCENE_DIR = MISSIONS_DIR / "scene_cache"
INBOX_DIR = MISSIONS_DIR / "inbox"        # drop a bundle .zip here and it is imported on the next listing

TRACK_MAX_POINTS = 8000
BATHY_GRID = 200                          # seabed grid points along the longer side
FLOAT_INDEX_PAD_DAYS = 2


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(s).lower()).strip("-") or "mission"


def _ms(date: str) -> float:
    """'2024-06-09' / '2024-06-09T05:10' (naive UTC) -> epoch ms."""
    d = str(date).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000.0
        except ValueError:
            continue
    raise ValueError(f"Bad date: {date!r}")


def _mission_files() -> dict:
    out = {}
    # user missions override bundled examples with the same id; a server lists only what was put in its missions folder
    for folder in ((MISSIONS_DIR,) if server_config.IS_SERVER else (EXAMPLES_DIR, MISSIONS_DIR)):
        if folder.is_dir():
            for f in sorted(folder.glob("*.json")):
                out[_slug(f.stem)] = f
    return out


def load(mission_id: str) -> dict | None:
    if _slug(mission_id).startswith(live_logic.PREFIX):
        return live_logic.missions().get(_slug(mission_id))
    f = _mission_files().get(_slug(mission_id))
    if not f:
        return None
    m = json.loads(f.read_text(encoding="utf-8"))
    m["id"] = _slug(f.stem)
    return m


def _file_index() -> dict:
    """Registered files by name; the same name registered twice resolves to the newest copy."""
    out = {}
    for rec in sorted(cache_logic.list_files(), key=lambda r: r.get("mtime", 0)):
        out[rec["name"].lower()] = rec
    return out


def _resolve(platform: dict, index: dict) -> dict | None:
    names = platform.get("file") or []
    names = [str(n).lower() for n in ([names] if isinstance(names, str) else names)]
    # Any listed name also matches its "<name>_Processed.nc" twin, and a processed copy wins over every plain one.
    processed = [n if n.endswith("_processed.nc") else n[:-3] + "_processed.nc" for n in names if n.endswith(".nc")]
    for name in dict.fromkeys(processed + names):
        rec = index.get(name)
        if rec:
            return rec
    return None


def resolved(m: dict) -> dict:
    """The mission with each platform's file status filled in (file_id / status / progress)."""
    index = _file_index()
    out = dict(m)
    out["platforms"] = []
    for p in m.get("platforms", []):
        rec = _resolve(p, index)
        out["platforms"].append({
            **p,
            "file_id": rec["id"] if rec else None,
            "status": rec["status"] if rec else "missing",
            "progress": rec.get("progress", 0) if rec else 0,
            "kind": p.get("model") or (rec or {}).get("platform_kind") or "slocum",
        })
    return out


def list_missions() -> list[dict]:
    from . import bundle
    bundle.import_inbox()
    index = _file_index()
    out = []
    for mid in [*live_logic.missions(), *_mission_files()]:
        try:
            m = load(mid)
        except Exception as e:  # noqa: BLE001
            logger.warning("Mission %s unreadable: %s", mid, e)
            continue
        plats = m.get("platforms", [])
        have = sum(1 for p in plats if _resolve(p, index))
        out.append({"id": mid, "title": " ".join(str(m.get("title", mid)).split()), "summary": m.get("summary", ""),
                    "platforms": len(plats), "platforms_available": have, "live": bool(m.get("live"))})
    return out


# ---------- tracks ----------

def track(m: dict, key: str, colour: str | None = None) -> dict:
    p = next((q for q in m.get("platforms", []) if q.get("key") == key), None)
    if not p:
        return {"status": "error", "error": "Unknown platform"}
    rec = _resolve(p, _file_index())
    if not rec:
        return {"status": "missing"}
    payload = cache_logic.get_payload(rec["id"], "spatial_3d")
    if payload is None:
        if rec.get("status") != cache_logic.STATUS_READY:
            return {"status": "processing", "progress": rec.get("progress", 0)}
        payload = spatial_logic.generate_3d_data(rec["path"])
    if not payload or payload.get("error") or not payload.get("lon"):
        return {"status": "error", "error": (payload or {}).get("error", "No position data")}

    t = np.asarray(payload["time_ms"], dtype=float)
    keep = np.isfinite(t)
    if p.get("from"):
        keep &= t >= _ms(p["from"])
    if p.get("to"):
        keep &= t <= _ms(p["to"])
    idx = np.flatnonzero(keep)
    if idx.size > TRACK_MAX_POINTS:
        idx = idx[np.unique(np.linspace(0, idx.size - 1, TRACK_MAX_POINTS).astype(int))]

    def col(name, nd):
        v = payload.get(name)
        if not v:
            return None
        a = np.asarray([np.nan if x is None else x for x in v], dtype=float)[idx]
        return [None if not np.isfinite(x) else round(float(x), nd) for x in a]

    out = {"status": "ready", "file_id": rec["id"], "lon": col("lon", 5), "lat": col("lat", 5), "z": col("elevation", 1),
           "time_ms": [float(x) for x in t[idx]], "pitch": col("pitch", 1)}
    if colour:
        c = spatial_logic.track_colour(rec["path"], colour)
        vals = c.get("values")
        want = (_colour_presets(m).get(colour) or {}).get("var")
        if vals and len(vals) == len(payload["lon"]) and str(c.get("var", "")).replace("_ADJUSTED", "") == want:
            out["colour"] = {"values": [vals[i] for i in idx], "units": c.get("units", ""), "var": c.get("var")}
    return out


def _colour_presets(m: dict) -> dict:
    """{preset key: option} for presets at least one loaded platform has. Where platforms disagree on the variable
    behind a preset (MOLAR_DOXY vs an ALR's raw FREQUENCY_DOXY), the commonest wins: one scale needs one quantity."""
    index = _file_index()
    seen: dict = {}
    for p in m.get("platforms", []):
        rec = _resolve(p, index)
        if not rec or rec.get("status") != cache_logic.STATUS_READY:
            continue
        for o in spatial_logic.track_colour_options(rec["path"]):
            if str(o.get("cmap", "")).startswith("discrete"):     # shared continuous scale only
                continue
            seen.setdefault(o["key"], []).append(o)
    base = lambda o: o["var"].replace("_ADJUSTED", "")
    out = {}
    for k, opts in seen.items():
        names = [base(o) for o in opts]
        win = max(names, key=names.count)          # ties: the first platform listed
        out[k] = {**next(o for o in opts if base(o) == win), "var": win}
    return out


def colour_options(m: dict) -> list:
    """Track-colour presets for the mission; platforms without a preset's variable draw grey."""
    return [{"key": k, "label": v["label"], "cmap": v["cmap"]} for k, v in _colour_presets(m).items()]


# ---------- floats ----------

def _float_tracks(m: dict, bounds: dict, t0: float, t1: float) -> list[dict]:
    """Argo floats for the scene, from the GDAC profile index the Argo layer already keeps.

    "floats": {"wmo": [...]}  lists floats outright;
    "floats": {"deployed_near": {"lat", "lon", "radius_km", "between": [date, date]}}  finds floats whose FIRST
    profile falls in that circle and window (i.e. floats launched there);
    "floats": {"dac": "bodc" | [...]}  takes every float of those data centres that surfaces in the scene during the
    mission ("all" = any centre). All three can be combined.
    """
    cfg = m.get("floats") or {}
    if not cfg:
        return []
    pad = FLOAT_INDEX_PAD_DAYS * 86400000.0
    res = argo_logic.profiles_in(bounds["min_lat"], bounds["max_lat"], bounds["min_lon"], bounds["max_lon"], t0 - pad, t1 + pad)
    if res.get("status") != "ready":
        return [{"status": res.get("status", "error")}]
    by_wmo: dict = {}
    for wmo, lat, lon, ms in res["profiles"]:
        by_wmo.setdefault(str(wmo), []).append([ms, lat, lon])
    wanted = {str(w) for w in cfg.get("wmo", [])}
    near = cfg.get("deployed_near")
    if near:
        n0, n1 = (_ms(d) for d in near["between"])
        # "First profile" must be judged against the float's whole life, not just this window.
        life = argo_logic.profiles_in(near["lat"] - 3, near["lat"] + 3, near["lon"] - 6, near["lon"] + 6, 0, n0 - 1)
        earlier = {str(r[0]) for r in life.get("profiles", [])}
        for wmo, profs in by_wmo.items():
            ms, lat, lon = min(profs)
            dist = 111.2 * np.hypot(lat - near["lat"], (lon - near["lon"]) * np.cos(np.radians(near["lat"])))
            if n0 <= ms <= n1 and dist <= near.get("radius_km", 50) and wmo not in earlier:
                wanted.add(wmo)
    dacs = cfg.get("dac")
    if dacs:
        dacs = {str(d).lower() for d in ([dacs] if isinstance(dacs, str) else dacs)}
        centre = {str(r[0]): str(r[6]).lower() for r in argo_logic.list_floats().get("floats", [])}
        wanted |= {w for w, profs in by_wmo.items() if ("all" in dacs or centre.get(w) in dacs) and any(t0 <= p[0] <= t1 for p in profs)}
    out = []
    for wmo in sorted(wanted):
        profs = sorted(by_wmo.get(wmo, []))
        if profs:
            out.append({"wmo": wmo, "time_ms": [p[0] for p in profs], "lat": [p[1] for p in profs], "lon": [p[2] for p in profs]})
    return out


# ---------- scene ----------

def _bounds(m: dict) -> dict:
    r = m.get("region") or {}
    if r.get("lat") and r.get("lon"):
        return {"min_lat": min(r["lat"]), "max_lat": max(r["lat"]), "min_lon": min(r["lon"]), "max_lon": max(r["lon"])}
    lats, lons = [], []
    index = _file_index()
    for p in m.get("platforms", []):
        rec = _resolve(p, index)
        b = (cache_logic.get_payload(rec["id"], "spatial_3d") or {}).get("bounds") if rec else None
        if b:
            lats += [b["min_lat"], b["max_lat"]]; lons += [b["min_lon"], b["max_lon"]]
    if not lats:
        raise ValueError("Mission has no 'region' and none of its files are available to derive one")
    pad_lat, pad_lon = (max(lats) - min(lats)) * 0.15 + 0.2, (max(lons) - min(lons)) * 0.15 + 0.4
    return {"min_lat": min(lats) - pad_lat, "max_lat": max(lats) + pad_lat, "min_lon": min(lons) - pad_lon, "max_lon": max(lons) + pad_lon}


def _bathy(bounds: dict, grid: int = BATHY_GRID) -> dict:
    """Strided seabed for the mission box (the full grid for a box this big would be tens of MB). The default is the
    Plotly page's 1-arc-minute ETOPO; a finer `grid` (the three.js page) reads the 15-arc-second ETOPO 2022."""
    from ..core import spatial_logic
    return spatial_logic.fetch_bathy_grid(bounds, grid, fine=grid > BATHY_GRID)


def scene(m: dict, grid: int = BATHY_GRID) -> dict:
    """Bounds + bathymetry + floats. Cached on disk per mission content and grid (the ETOPO fetch for a big box is slow)."""
    bounds = _bounds(m)
    t = m.get("time") or {}
    t0, t1 = _ms(t["start"]), _ms(t["end"])
    sig = hashlib.sha1(json.dumps([bounds, m.get("floats"), t0, t1, grid], sort_keys=True).encode()).hexdigest()[:16]
    SCENE_DIR.mkdir(parents=True, exist_ok=True)
    cache = SCENE_DIR / f"{m['id']}_{grid}_{sig}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:  # noqa: BLE001
            pass
    out = {"bounds": bounds, "time_ms": [t0, t1], **_bathy(bounds, grid), "floats": _float_tracks(m, bounds, t0, t1)}
    if not any("status" in f for f in out["floats"]):      # don't pin a scene made while the Argo index was still building
        for stale in SCENE_DIR.glob(f"{m['id']}_{grid}_*.json"):
            stale.unlink(missing_ok=True)
        cache.write_text(json.dumps(out))
    return out


# ---------- list preview ----------

PREVIEW_POINTS = 160


def preview(m: dict) -> dict:
    """Small payload for the mission list's map thumbnail: region, dates, thinned tracks, ship legs, stations."""
    index = _file_index()
    lines, kinds = [], {}
    for p in m.get("platforms", []):
        rec = _resolve(p, index)
        kind = p.get("model") or (rec or {}).get("platform_kind") or "slocum"
        kinds[kind] = kinds.get(kind, 0) + 1
        payload = cache_logic.get_payload(rec["id"], "spatial_3d") if rec else None
        if not payload or not payload.get("lon"):
            continue
        t = np.asarray(payload["time_ms"], dtype=float)
        keep = np.isfinite(t)
        if p.get("from"):
            keep &= t >= _ms(p["from"])
        if p.get("to"):
            keep &= t <= _ms(p["to"])
        idx = np.flatnonzero(keep)
        if idx.size < 2:
            continue
        idx = idx[np.unique(np.linspace(0, idx.size - 1, PREVIEW_POINTS).astype(int))]
        pts = [[round(float(payload["lat"][i]), 3), round(float(payload["lon"][i]), 3)] for i in idx
               if payload["lat"][i] is not None and payload["lon"][i] is not None]
        lines.append({"colour": p.get("colour", "#d9c45c"), "width": 2.2 if p.get("width") else 1.4, "points": pts})
    ships = [{"colour": sh.get("colour", "#e8702a"), "label": sh.get("label", "Ship"), "model": sh.get("model", "rrs_discovery"),
              "legs": [leg["points"] for leg in sh.get("legs", []) if leg.get("points")]} for sh in m.get("ships", [])]
    try:
        bounds = _bounds(m)
    except Exception:  # noqa: BLE001
        bounds = None
    return {"bounds": bounds, "time": m.get("time"), "lines": lines, "ships": ships, "kinds": kinds,
            "floats": bool(m.get("floats")), "stations": m.get("stations", [])}
