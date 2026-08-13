"""Copernicus Marine surface overlays — fetch + lightweight session cache.

Generalises the original CHL-a overlay to any registered variable (chlorophyll,
temperature, salinity, oxygen, pH, phytoplankton biomass, sea level anomaly). Each overlay maps to
one or more Copernicus dataset ids and a variable name; the satellite ocean-colour
and altimetry SLA products are L4 (2D), the rest are 3D model analysis/forecast
products from which we take the surface level. The map view draws the returned
point grid as coloured cells on the globe.
"""

import json
import logging
import re
import struct
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# The copernicusmarine toolbox stores credentials in a single file under the
# user's home dir, independent of the Python environment — so a login done in
# any terminal/env, or via login() below, is visible everywhere.
_CONFIG_DIR = Path.home() / ".copernicusmarine"
_CREDENTIALS_FILE = _CONFIG_DIR / ".copernicusmarine-credentials"


def credentials_present() -> bool:
    """True if a Copernicus Marine credentials file exists for this user."""
    return _CREDENTIALS_FILE.is_file()


def login(username: str, password: str) -> dict:
    """Validate the given Copernicus Marine credentials online and, if valid,
    persist them to ~/.copernicusmarine so every subsequent overlay fetch is
    authenticated. No terminal and no restart needed — open_dataset reads the
    credentials file per call, so a fresh login is picked up immediately. Returns
    {"status": "success"} or {"status": "error", "message": ...}."""
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        return {"status": "error", "message": "Username and password are required"}
    try:
        import copernicusmarine
    except ImportError:
        return {"status": "error", "message": "copernicusmarine package is not installed"}
    try:
        # check_credentials_valid=True verifies online before writing: invalid
        # creds return False and leave any existing file untouched.
        ok = copernicusmarine.login(
            username=username,
            password=password,
            force_overwrite=True,
            check_credentials_valid=True,
        )
    except Exception as exc:
        logger.warning("Copernicus login error: %s", exc)
        return {"status": "error", "message": f"Login failed: {exc}"}
    if not ok:
        return {"status": "error", "message": "Invalid Copernicus Marine username or password"}
    logger.info("Copernicus Marine credentials saved for %s", username)
    return {"status": "success"}

# Satellite ocean-colour: near-real-time (last ~1y) then multi-year reprocessed.
_DS_CHL_NRT = "cmems_obs-oc_glo_bgc-plankton_nrt_l4-gapfree-multi-4km_P1D"
_DS_CHL_MY = "cmems_obs-oc_glo_bgc-plankton_my_l4-gapfree-multi-4km_P1D"

# Surface currents: eastward (uo) + northward (vo) velocity, analysis/forecast.
_DS_CUR = "cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m"

# Sea level anomaly (DUACS L4 altimetry): higher-res near-real-time first, then
# the coarser near-real-time and reprocessed multi-year products as fallbacks for
# older dates the 0.125deg NRT product doesn't cover.
# `sla` is the height anomaly relative to the 20-year mean — this is the quantity
# Copernicus users plot, not the model `zos` (height above geoid) we showed before.
_DS_SLA_NRT_HI = "cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.125deg_P1D"
_DS_SLA_NRT = "cmems_obs-sl_glo_phy-ssh_nrt_allsat-l4-duacs-0.25deg_P1D"
_DS_SLA_MY = "cmems_obs-sl_glo_phy-ssh_my_allsat-l4-duacs-0.25deg_P1D"

# Registry of available overlays. `surface` marks the 3D model products whose
# top depth level we extract. Order here is the order shown in the map legend.
OVERLAYS: dict[str, dict] = {
    "chla":     {"datasets": [_DS_CHL_NRT, _DS_CHL_MY], "variable": "CHL",    "surface": False},
    "temp":     {"datasets": ["cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m"], "variable": "thetao", "surface": True},
    "salinity": {"datasets": ["cmems_mod_glo_phy-so_anfc_0.083deg_P1D-m"],     "variable": "so",     "surface": True},
    "o2":       {"datasets": ["cmems_mod_glo_bgc-bio_anfc_0.25deg_P1D-m"],     "variable": "o2",     "surface": True},
    "ph":       {"datasets": ["cmems_mod_glo_bgc-car_anfc_0.25deg_P1D-m"],     "variable": "ph",     "surface": True},
    "biomass":  {"datasets": ["cmems_mod_glo_bgc-pft_anfc_0.25deg_P1D-m"],     "variable": "phyc",   "surface": True},
    "ssh":      {"datasets": [_DS_SLA_NRT_HI, _DS_SLA_NRT, _DS_SLA_MY], "variable": "sla", "surface": False, "demean": True},
}

# Currents is a vector field (uo, vo) rather than a single scalar, so it gets its
# own spec and fetch path. The frontend draws a speed colour mesh plus an animated
# particle flow advected through the u/v grid.
CURRENTS: dict = {"dataset": _DS_CUR, "variables": ["uo", "vo"]}

# Session-level LRU dict: key → result; avoids re-fetching on repeat toggle.
_CACHE: dict = {}
_MAX_CACHE = 24


def warm_up() -> None:
    """Pre-import copernicusmarine so the first overlay request doesn't pay its
    ~2s cold-import cost inline (it otherwise lands as unattributed "other" time
    on the first overlay of a session). Safe to call from a background thread at
    startup; a missing/broken package is ignored here — the real fetch re-imports
    and surfaces any error to the user."""
    try:
        import copernicusmarine  # noqa: F401
    except Exception:
        pass

# Keep the cell grid under this per side so the globe stays responsive. The
# satellite CHL-a grid is 1/24° (≈4 km), so 800 keeps a box of up to ~33° per
# side at full native resolution — in particular a cos(lat)-widened box at high
# latitude (see _fetch_cached) stays native instead of being subsampled. The
# merged-mesh overlay handles this cell count easily; the payload gzips ~5x.
_MAX_CELLS_PER_SIDE = 800

# Currents are subsampled harder: the field is smooth, the payload carries two
# components per cell, and the frontend interpolates a continuous flow from it —
# so a coarser grid is plenty and keeps the particle advection cheap.
_MAX_CURRENT_CELLS_PER_SIDE = 160


def _cache_key(var, lat_min, lat_max, lon_min, lon_max, date_str):
    # Round to 0.5° so small pan differences hit the same entry.
    return (var, round(lat_min * 2) / 2, round(lat_max * 2) / 2,
            round(lon_min * 2) / 2, round(lon_max * 2) / 2, date_str)


def fetch_overlay(
    var: str,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    target_date: str | None = None,
) -> dict:
    """
    Return a point grid of `var` for the bounding box.

    On success: {"points": [[lat, lng, value], ...], "date": "YYYY-MM-DD",
                 "p10": float, "p90": float, "half_deg": float, "units": str}
    On failure: {"error": str, "hint": str}
    """
    spec = OVERLAYS.get(var)
    if spec is None:
        return {"error": f"Unknown overlay '{var}'", "hint": ""}

    return _fetch_cached(
        var, lat_min, lat_max, lon_min, lon_max, target_date,
        lambda cm, lo, la, lo2, la2, d, tm: _fetch(cm, spec, lo, la, lo2, la2, d, tm),
        size_of=lambda r: f"{len(r['points'])} points",
    )


def fetch_currents(
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    target_date: str | None = None,
) -> dict:
    """
    Return the surface current (uo, vo) field over the bounding box as a regular
    lat/lon grid the frontend can interpolate for particle advection.

    On success: {"lat0", "lon0", "dlat", "dlon", "nlat", "nlon",
                 "u": [[..]], "v": [[..]],  # nlat x nlon, null where masked
                 "date", "speed_p90", "speed_max", "half_deg", "units"}
    On failure: {"error": str, "hint": str}
    """
    return _fetch_cached(
        "currents", lat_min, lat_max, lon_min, lon_max, target_date,
        _fetch_currents,
        size_of=lambda r: f"{r['nlat']}x{r['nlon']} grid",
    )


def _fetch_cached(var, lat_min, lat_max, lon_min, lon_max, target_date, runner, size_of):
    """Shared bbox-pad + date-default + session-cache wrapper around a fetch run.

    `runner(cm, min_lat, max_lat, min_lon, max_lon, date_str, timing)` does the
    actual Copernicus call and returns a result dict (or {"error": ...}). It
    accumulates per-phase seconds into `timing` (download/extract) across *every*
    attempt — failed opens, date-cap retries and fallback datasets included — so
    a wrong-dataset miss shows up under "download" rather than as "other".
    """
    try:
        import copernicusmarine
    except ImportError:
        return {
            "error": "copernicusmarine package is not installed",
            "hint": "Run: pip install copernicusmarine",
            "setup": "install",
        }

    # Fetch a ~12° (TARGET) box of latitude centred on the deployment, and a
    # longitude span widened by 1/cos(lat) so the box covers a *physically* square
    # area rather than a square-in-degrees one. A degree of longitude shrinks
    # toward the poles, so without this a high-latitude box looks tall and narrow
    # on the globe (more coverage N-S than E-W). At 1/24° (≈4 km native) a box this
    # size stays at full resolution thanks to _MAX_CELLS_PER_SIDE=800; the payload
    # is ~0.7 MB gzipped and the fetch is sub-second beyond the open handshake —
    # see the overlay size audit. A larger deployment expands the box to cover
    # itself plus a small margin; a point/small deployment still gets the full box.
    t_prep = time.time()   # bbox padding + date defaulting (server prep)
    TARGET = 12.0   # degrees of latitude per side for a typical deployment
    MARGIN = 2.0    # extra context when the deployment already exceeds TARGET
    lat_c = (lat_min + lat_max) / 2.0
    lon_c = (lon_min + lon_max) / 2.0
    half_lat = max(TARGET / 2.0, (lat_max - lat_min) / 2.0 + MARGIN)
    # Clamp cos(lat) so the longitude widening can't blow up near the poles
    # (cos 0.3 ≈ 72.5° lat); also cap the half-span at 16° so even the widened
    # box stays within the native-resolution limit of the 800-cell cap.
    cos_lat = max(float(np.cos(np.radians(lat_c))), 0.3)
    half_lon = max(half_lat / cos_lat, (lon_max - lon_min) / 2.0 + MARGIN)
    half_lon = min(half_lon, 16.0)
    min_lat = max(-90.0, lat_c - half_lat)
    max_lat = min(90.0, lat_c + half_lat)
    min_lon = max(-180.0, lon_c - half_lon)
    max_lon = min(180.0, lon_c + half_lon)

    if target_date:
        date_str = target_date[:10]
    else:
        date_str = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")

    prep = time.time() - t_prep

    key = _cache_key(var, min_lat, max_lat, min_lon, max_lon, date_str)
    if key in _CACHE:
        logger.info("Overlay %s cache hit for %s", var, date_str)
        print(f"[{var}] Cache hit for {date_str}", flush=True)
        # Don't mutate the cached object — hand back a shallow copy whose
        # `_timing` reflects *this* (cache-hit) request, not the original fetch.
        out = dict(_CACHE[key])
        out["_timing"] = {"prep": prep, "cache_hit": True}
        return out

    print(f"[{var}] Fetching for date={date_str} bbox=({min_lat:.1f},{min_lon:.1f})-({max_lat:.1f},{max_lon:.1f})", flush=True)
    timing = {"download": 0.0, "extract": 0.0, "attempts": 0}
    t0 = time.time()
    result = runner(copernicusmarine, min_lat, max_lat, min_lon, max_lon, date_str, timing)
    elapsed = time.time() - t0
    if "error" in result:
        print(f"[{var}] Fetch failed in {elapsed:.1f}s: {result['error']}", flush=True)
    else:
        print(f"[{var}] Fetch OK in {elapsed:.1f}s — {size_of(result)}, date={result['date']}", flush=True)
        # Attach the accumulated download/extract phases plus prep for the client.
        timing["prep"] = prep
        result["_timing"] = timing
        if len(_CACHE) >= _MAX_CACHE:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = result

    return result


def pack_overlay_response(result: dict) -> bytes:
    """Pack a successful scalar-overlay result into the binary container the map
    view decodes — the same layout as the plot binary so the frontend reuses the
    same decode path:

        uint32 LE  header length
        JSON       header: scalar metadata (date/p10/p90/half_deg/units/n/_timing)
                   plus an `arrays` descriptor {lat,lon,val: {dtype, len}}
        bytes      lat, lon, val as raw little-endian float32, concatenated

    This replaces a ~100k-element JSON list of [lat,lng,val] triples: the server
    skips the JSON text encode and the browser maps the bytes straight into
    Float32Arrays instead of JSON.parse-ing hundreds of thousands of numbers.
    """
    pts = np.ascontiguousarray(result["points"], dtype=np.float32)
    n = int(pts.shape[0])
    header = {
        "date": result["date"],
        "p10": result["p10"],
        "p90": result["p90"],
        "half_deg": result["half_deg"],
        "units": result.get("units", ""),
        "n": n,
        "_timing": result.get("_timing"),
        "arrays": {
            "lat": {"dtype": "f32", "len": n},
            "lon": {"dtype": "f32", "len": n},
            "val": {"dtype": "f32", "len": n},
        },
    }
    hjson = json.dumps(header).encode("utf-8")
    body = b"".join(np.ascontiguousarray(pts[:, i]).tobytes() for i in range(3))
    return b"".join([struct.pack("<I", len(hjson)), hjson, body])


# ---------- internals ----------

def _fetch(cm, spec, min_lat, max_lat, min_lon, max_lon, date_str, timing):
    """Try each candidate scalar dataset; cap the date to its max on overrun."""
    return _try_datasets(
        spec["datasets"],
        lambda dataset_id, d: _open_and_extract(
            cm, dataset_id, spec, min_lat, max_lat, min_lon, max_lon, d, timing),
        date_str,
    )


def _fetch_currents(cm, min_lat, max_lat, min_lon, max_lon, date_str, timing):
    """Fetch the uo/vo current grid, with the same auth/bounds handling."""
    return _try_datasets(
        [CURRENTS["dataset"]],
        lambda dataset_id, d: _open_and_extract_vec(
            cm, dataset_id, min_lat, max_lat, min_lon, max_lon, d, timing),
        date_str,
    )


def _try_datasets(dataset_ids, open_fn, date_str):
    """Run `open_fn(dataset_id, date)` over candidates, translating Copernicus
    auth failures and date-out-of-range errors (retrying at the capped date)."""
    last_err = None
    for dataset_id in dataset_ids:
        try:
            return open_fn(dataset_id, date_str)
        except Exception as exc:
            msg = str(exc)
            logger.warning("Overlay fetch error (dataset=%s): %s", dataset_id, msg)

            if _is_auth_error(msg):
                return {
                    "error": "Copernicus Marine authentication failed",
                    "hint": "Enter your Copernicus Marine account details to sign in.",
                    "setup": "login",
                }

            # If our date is beyond the dataset's range, retry at its actual max.
            if _is_bounds_error(msg):
                capped = _parse_max_date(msg)
                if capped and capped != date_str:
                    logger.info("Date %s exceeds range — retrying with %s", date_str, capped)
                    try:
                        return open_fn(dataset_id, capped)
                    except Exception as exc2:
                        logger.warning("Overlay retry error (dataset=%s, date=%s): %s",
                                       dataset_id, capped, exc2)
                        last_err = str(exc2)
                        continue

            last_err = msg

    return {
        "error": f"Could not retrieve data: {last_err}",
        "hint": "If this is a sign-in problem, enter your Copernicus Marine details below.",
        "setup": "login",
    }


def _open_and_extract(cm, dataset_id, spec, min_lat, max_lat, min_lon, max_lon, date_str, timing=None):
    logger.info("Fetching %s from %s for %s", spec["variable"], dataset_id, date_str)
    print(f"[{spec['variable']}] open_dataset {dataset_id} …", flush=True)
    t0 = time.time()
    kwargs = dict(
        dataset_id=dataset_id,
        variables=[spec["variable"]],
        minimum_latitude=min_lat,
        maximum_latitude=max_lat,
        minimum_longitude=min_lon,
        maximum_longitude=max_lon,
        start_datetime=f"{date_str}T00:00:00",
        end_datetime=f"{date_str}T23:59:59",
    )
    if spec.get("surface"):
        # Only pull the shallowest level of the 3D model grid.
        kwargs["minimum_depth"] = 0.0
        kwargs["maximum_depth"] = 1.0
    # Time the open even when it raises (wrong-dataset / out-of-range miss), so a
    # failed attempt's network cost is still attributed to "download".
    try:
        ds = cm.open_dataset(**kwargs)
    finally:
        if timing is not None:
            timing["download"] = timing.get("download", 0.0) + (time.time() - t0)
            timing["attempts"] = timing.get("attempts", 0) + 1
    t_open = time.time() - t0
    print(f"[{spec['variable']}] open_dataset done in {t_open:.1f}s", flush=True)
    t1 = time.time()
    result = _extract(ds, spec["variable"], date_str, demean=spec.get("demean", False))
    if timing is not None:
        timing["extract"] = timing.get("extract", 0.0) + (time.time() - t1)
    return result


def _open_and_extract_vec(cm, dataset_id, min_lat, max_lat, min_lon, max_lon, date_str, timing=None):
    variables = CURRENTS["variables"]
    logger.info("Fetching currents %s from %s for %s", variables, dataset_id, date_str)
    print(f"[currents] open_dataset {dataset_id} …", flush=True)
    t0 = time.time()
    try:
        ds = cm.open_dataset(
            dataset_id=dataset_id,
            variables=variables,
            minimum_latitude=min_lat,
            maximum_latitude=max_lat,
            minimum_longitude=min_lon,
            maximum_longitude=max_lon,
            start_datetime=f"{date_str}T00:00:00",
            end_datetime=f"{date_str}T23:59:59",
            minimum_depth=0.0,
            maximum_depth=1.0,
        )
    finally:
        if timing is not None:
            timing["download"] = timing.get("download", 0.0) + (time.time() - t0)
            timing["attempts"] = timing.get("attempts", 0) + 1
    t_open = time.time() - t0
    print(f"[currents] open_dataset done in {t_open:.1f}s", flush=True)
    t1 = time.time()
    result = _extract_vec(ds, variables, date_str)
    if timing is not None:
        timing["extract"] = timing.get("extract", 0.0) + (time.time() - t1)
    return result


def _is_auth_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in ("401", "403", "unauthorized", "forbidden",
                                  "credentials", "login required", "authentication"))


def _is_bounds_error(msg: str) -> bool:
    low = msg.lower()
    return "exceed" in low and "dataset coordinates" in low


def _parse_max_date(msg: str) -> str | None:
    """Pull the dataset's max available date out of a bounds-exceeded message."""
    m = re.search(r"dataset coordinates\s*\[.*?,\s*(\d{4}-\d{2}-\d{2})", msg)
    return m.group(1) if m else None


def _extract(ds, variable: str, date_str: str, demean: bool = False) -> dict:
    """Flatten the (surface) field into a list of [lat, lng, value] cells.

    Subsamples by an adaptive stride so the payload stays small and the globe's
    overlay mesh is fast to build — at most _MAX_CELLS_PER_SIDE cells per axis.

    `demean` subtracts the box spatial mean from every cell (used for SLA): the
    altimetry anomaly carries a basin-scale offset, so removing the regional mean
    centres the diverging ramp on zero and surfaces the local mesoscale structure
    (eddies, fronts) — matching how Copernicus users plot `sla - sla.mean(...)`.
    """
    var_key = next((k for k in ds.data_vars if k.lower() == variable.lower()), None)
    if var_key is None:
        return {"error": f"Variable '{variable}' not found in dataset", "hint": ""}

    arr = ds[var_key]
    units = str(arr.attrs.get("units", "") or "")

    # Collapse time and depth down to a single 2D slice (surface, first time).
    for dim in ("time", "depth", "elevation"):
        if dim in arr.dims:
            arr = arr.isel({dim: 0})

    lat_key = next((k for k in ds.coords if k.lower() in ("latitude", "lat")), None)
    lon_key = next((k for k in ds.coords if k.lower() in ("longitude", "lon")), None)
    if lat_key is None or lon_key is None:
        return {"error": "Could not find lat/lon coordinates in dataset", "hint": ""}

    lats = ds[lat_key].values.astype(float)
    lons = ds[lon_key].values.astype(float)
    vals = np.array(arr.values, dtype=float)

    longest = max(len(lats), len(lons), 1)
    stride = max(1, int(np.ceil(longest / _MAX_CELLS_PER_SIDE)))
    lats = lats[::stride]
    lons = lons[::stride]
    vals = vals[::stride, ::stride]

    half_deg = float(abs(lats[1] - lats[0]) / 2.0) if len(lats) >= 2 else 0.05

    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")
    mask = np.isfinite(vals)

    if not np.any(mask):
        return {
            "error": "No valid data for this region/date (all masked)",
            "hint": "The field may be clouded or off-grid — try a different region",
        }

    flat = vals[mask]
    if demean:
        flat = flat - float(np.mean(flat))
        # SLA's diverging ramp saturates hard at the percentile-based range —
        # eddies routinely exceed it. Copernicus users read SLA on a fixed
        # +/-0.2 m scale, so match that instead of the data's own p10/p90.
        p10, p90 = -0.2, 0.2
    else:
        p10 = float(np.percentile(flat, 10))
        p90 = float(np.percentile(flat, 90))

    # Pack [lat, lng, value] as a float32 (n, 3) array, fully vectorised — this
    # skips the old per-cell Python list build (the slow part of "extract" for a
    # 100k+ cell field) and gives the route a typed array it can ship as raw
    # bytes instead of a giant JSON list. float32 matches the source precision.
    points = np.empty((int(np.count_nonzero(mask)), 3), dtype=np.float32)
    points[:, 0] = np.round(lat_grid[mask], 4)
    points[:, 1] = np.round(lon_grid[mask], 4)
    points[:, 2] = np.round(flat, 5)
    logger.info("Overlay %s: %d points for %s (p10=%.3f p90=%.3f, stride=%d)",
                variable, len(points), date_str, p10, p90, stride)

    return {"points": points, "date": date_str, "p10": p10, "p90": p90,
            "half_deg": half_deg, "units": units}


def _extract_vec(ds, variables, date_str: str) -> dict:
    """Flatten the surface (uo, vo) field into a regular lat/lon grid.

    Unlike `_extract`, currents keep their grid shape (nlat x nlon) so the
    frontend can bilinearly interpolate the flow when advecting particles.
    Masked cells (land / no data) become null. The grid is returned south→north
    and west→east with positive dlat/dlon for simple index arithmetic in JS.
    """
    keys = []
    for v in variables:
        k = next((kk for kk in ds.data_vars if kk.lower() == v.lower()), None)
        if k is None:
            return {"error": f"Variable '{v}' not found in dataset", "hint": ""}
        keys.append(k)

    units = str(ds[keys[0]].attrs.get("units", "") or "m s-1")

    arrs = []
    for k in keys:
        a = ds[k]
        for dim in ("time", "depth", "elevation"):
            if dim in a.dims:
                a = a.isel({dim: 0})
        arrs.append(a)

    lat_key = next((k for k in ds.coords if k.lower() in ("latitude", "lat")), None)
    lon_key = next((k for k in ds.coords if k.lower() in ("longitude", "lon")), None)
    if lat_key is None or lon_key is None:
        return {"error": "Could not find lat/lon coordinates in dataset", "hint": ""}

    lats = ds[lat_key].values.astype(float)
    lons = ds[lon_key].values.astype(float)
    U = np.array(arrs[0].values, dtype=float)
    V = np.array(arrs[1].values, dtype=float)

    longest = max(len(lats), len(lons), 1)
    stride = max(1, int(np.ceil(longest / _MAX_CURRENT_CELLS_PER_SIDE)))
    lats = lats[::stride]
    lons = lons[::stride]
    U = U[::stride, ::stride]
    V = V[::stride, ::stride]

    # Normalise to ascending lat/lon so dlat/dlon are positive in the payload.
    if len(lats) >= 2 and lats[1] < lats[0]:
        lats = lats[::-1]
        U = U[::-1, :]
        V = V[::-1, :]
    if len(lons) >= 2 and lons[1] < lons[0]:
        lons = lons[::-1]
        U = U[:, ::-1]
        V = V[:, ::-1]

    speed = np.hypot(U, V)
    mask = np.isfinite(U) & np.isfinite(V)
    if not np.any(mask):
        return {
            "error": "No current data for this region/date (all masked)",
            "hint": "The model field may be off-grid here — try a different region",
        }

    sp = speed[mask]
    speed_p90 = float(np.percentile(sp, 90))
    speed_max = float(np.max(sp))

    # nlat x nlon nested lists, null where masked (JSON has no NaN).
    U_clean = np.where(mask, np.round(U, 4), None)
    V_clean = np.where(mask, np.round(V, 4), None)
    u_grid = [[None if x is None else float(x) for x in row] for row in U_clean]
    v_grid = [[None if x is None else float(x) for x in row] for row in V_clean]

    dlat = float(lats[1] - lats[0]) if len(lats) >= 2 else 0.083
    dlon = float(lons[1] - lons[0]) if len(lons) >= 2 else 0.083

    logger.info("Currents: %dx%d grid for %s (speed p90=%.3f max=%.3f, stride=%d)",
                len(lats), len(lons), date_str, speed_p90, speed_max, stride)

    return {
        "lat0": float(lats[0]), "lon0": float(lons[0]),
        "dlat": dlat, "dlon": dlon,
        "nlat": len(lats), "nlon": len(lons),
        "u": u_grid, "v": v_grid,
        "date": date_str, "speed_p90": speed_p90, "speed_max": speed_max,
        "half_deg": float(abs(dlat) / 2.0), "units": units,
    }
