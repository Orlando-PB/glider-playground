"""The glider's own track: position QC, and the map + 3D view payloads.

Reads lat / lon / pressure / temperature, trims position outliers and
decimates to a point budget. The map and 3D view share these arrays via
`get_core_spatial_data`, cached per file so switching between them is free.

Also: the 3D view payload (track, attitude, bathymetry), depth-averaged
current vectors, nearest-fix lookups, location summary, and KMZ export.
Map *layers* (Copernicus, Argo, ships, waypoints) live in maps/ instead.
"""

import functools
import io
import json
import os
import time
import zipfile
from xml.sax.saxutils import escape as _xml_escape

import numpy as np
import pandas as pd
import requests
from netCDF4 import Dataset

from . import plot_logic
from . import presets_logic

# Points per globe track — the same locally and on the server. Lower it here if
# the Pi's globe ever feels heavy with many tracks loaded (bump CACHE_VERSION).
MAX_POINTS = 5000
# The 3D view carries no colour data any more, so it can afford a denser
# track than the map: the model is interpolated between samples, so this
# mostly sharpens dive shapes and pitch/roll changes. Separate cache entry.
MAX_POINTS_3D = 20000
BATHY_RESOLUTION = 40
GEO_GAP_THRESHOLD_KM = 100.0
GEO_GAP_THRESHOLD_SEC = 2 * 86400.0   # 2 days
GEO_GROUP_MIN_POINTS = 100

# Position variable names in priority order. Some files lack the OG1 standard
# LATITUDE/LONGITUDE and instead carry the BODC parameter codes ALATPT01 /
# ALONPT01, which hold the same fix — use them as direct replacements. Others
# only carry the raw GPS fix under LATITUDE_GPS / LONGITUDE_GPS; fall back to
# that last so CTD derivation etc. still get a position at each row.
LAT_NAMES = ("LATITUDE", "ALATPT01", "LATITUDE_GPS")
LON_NAMES = ("LONGITUDE", "ALONPT01", "LONGITUDE_GPS")


def _resolve_latlon_names(container):
    """Pick the lat & lon variable names present in ``container`` (a dict or a
    netCDF ``variables`` mapping), preferring LATITUDE/LONGITUDE and falling
    back to the BODC codes ALATPT01/ALONPT01. Returns ``(lat_name, lon_name)``;
    either may be ``None`` if absent."""
    lat = next((n for n in LAT_NAMES if n in container), None)
    lon = next((n for n in LON_NAMES if n in container), None)
    return lat, lon

# Depth-averaged current (DAC). Gliders log one current estimate per dive
# (the dead-reckoning correction), so these arrays are sparse along the full
# record. Source variable pairs are tried in priority order; the third value
# scales the file's units to m/s.
DAC_VARIABLE_SETS = (
    ("WATER_VELOC_FINAL_U", "WATER_VELOC_FINAL_V", 1.0),    # m/s, surface-drift corrected
    ("WATERCURRENTS_U", "WATERCURRENTS_V", 0.01),           # cm/s -> m/s
)
DAC_MAX_VECTORS = 300
DAC_MAX_SPEED_MS = 5.0  # anything faster than this is a bad fix, not a real current
DAC_MATCH_MAX_SEC = 3600.0      # skip a current if no GPS fix within 1 h of it
DAC_MIN_INTERVAL_SEC = 6 * 3600.0  # thin to at most one arrow per ~6 h

# Set by cache_logic during prewarm to report sub-steps in real time.
_spatial_stage_cb = None


def _report_spatial_stage(msg: str):
    if _spatial_stage_cb is not None:
        try:
            _spatial_stage_cb(msg)
        except Exception:
            pass


# ---------- Geographic outlier ----------

def _trim_position_outliers(lat, lon, times=None):
    """Drop isolated stray fixes while keeping the whole real track.

    Splits the track into groups wherever there is a real break, then keeps
    *every* group with at least GEO_GROUP_MIN_POINTS fixes. A glider
    legitimately leaves big gaps behind (a long transit dive, a comms outage),
    so keeping only the single largest group used to discard whole later legs of
    the deployment. Only genuinely tiny clusters — a lone bad GPS fix flung far
    from the track — are dropped.

    A gap only counts as a *break* when it is large in BOTH distance (over
    GEO_GAP_THRESHOLD_KM) AND time (over GEO_GAP_THRESHOLD_SEC). A fast >100 km
    jump within a couple of days is normal transit, and a long pause that barely
    moves is a comms outage — neither should chop the track and risk hiding a
    legitimate leg. When times are unavailable we fall back to distance alone.
    """
    n = len(lat)
    valid = np.zeros(n, dtype=bool)

    if n < 2:
        return np.ones(n, dtype=bool)

    cos_lat = np.cos(np.deg2rad(lat[:-1]))
    dy = (lat[1:] - lat[:-1]) * 110.574
    dx = (lon[1:] - lon[:-1]) * 111.320 * cos_lat
    dist = np.hypot(dx, dy)

    is_break = dist > GEO_GAP_THRESHOLD_KM
    if times is not None and len(times) == n:
        dt = np.asarray(times[1:], dtype=float) - np.asarray(times[:-1], dtype=float)
        big_time = np.isfinite(dt) & (dt > GEO_GAP_THRESHOLD_SEC)
        is_break = is_break & big_time

    gap_indices = np.where(is_break)[0] + 1
    groups = np.split(np.arange(n), gap_indices)

    if not groups:
        return valid

    kept = [g for g in groups if len(g) >= GEO_GROUP_MIN_POINTS]
    if not kept:
        # Very short track — nothing clears the bar, so keep the largest
        # group rather than returning an empty path.
        kept = [max(groups, key=len)]

    for g in kept:
        valid[g] = True

    return valid

# ---------- Bathymetry ----------

@functools.lru_cache(maxsize=32)
def _fetch_bathy_cached(min_lon: float, max_lon: float, min_lat: float, max_lat: float):
    # ERDDAP snaps each bound to its NEAREST grid point (1 arc-minute), which can land inside the
    # request — widen by a cell so the grid (= the 3D scene box) always contains the track.
    cell = 1 / 60
    url = (
        "https://coastwatch.pfeg.noaa.gov/erddap/griddap/etopo180.csv"
        f"?altitude[({max(min_lat - cell, -90):.4f}):({min(max_lat + cell, 90):.4f})]"
        f"[({max(min_lon - cell, -180):.4f}):({min(max_lon + cell, 180):.4f})]"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text), skiprows=[1]).dropna(subset=["altitude"])
    lats = np.sort(df["latitude"].unique())
    lons = np.sort(df["longitude"].unique())

    lat_step = max(1, len(lats) // BATHY_RESOLUTION)
    lon_step = max(1, len(lons) // BATHY_RESOLUTION)
    # Subsample, always keeping the last row/column so the far edges aren't trimmed.
    lats = np.unique(np.append(lats[::lat_step], lats[-1]))
    lons = np.unique(np.append(lons[::lon_step], lons[-1]))

    df = df[df["latitude"].isin(lats) & df["longitude"].isin(lons)]
    pivot = df.pivot(index="latitude", columns="longitude", values="altitude") \
              .reindex(index=lats, columns=lons)

    return lons.tolist(), lats.tolist(), pivot.values.tolist()


def fetch_bathy_grid(bounds: dict, grid: int, fine: bool = True) -> dict:
    """Seabed grid for a lat/lon box, strided server-side to about `grid` points along the longer side. `fine` reads
    the 15-arc-second ETOPO 2022 (the three.js views); otherwise the 1-arc-minute ETOPO the Plotly views use. The box
    is widened by a cell so it always contains `bounds`. Heights are whole metres, 0 where missing."""
    dataset, var, per_deg = ("ETOPO_2022_v1_15s", "z", 240) if fine else ("etopo180", "altitude", 60)
    span = max(bounds["max_lat"] - bounds["min_lat"], bounds["max_lon"] - bounds["min_lon"])
    stride = max(1, int(round(span * per_deg / grid)))
    cell = stride / per_deg
    url = (f"https://coastwatch.pfeg.noaa.gov/erddap/griddap/{dataset}.csv"
           f"?{var}[({max(bounds['min_lat'] - cell, -90):.4f}):{stride}:({min(bounds['max_lat'] + cell, 90):.4f})]"
           f"[({max(bounds['min_lon'] - cell, -180):.4f}):{stride}:({min(bounds['max_lon'] + cell, 180):.4f})]")
    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    rows = np.genfromtxt(io.StringIO(resp.text), delimiter=",", skip_header=2)      # latitude-major, both ascending
    lats, lons = np.unique(rows[:, 0]), np.unique(rows[:, 1])
    z = np.nan_to_num(rows[:, 2]).reshape(len(lats), len(lons)).round().astype(int)
    return {"bathy_lon": lons.tolist(), "bathy_lat": lats.tolist(), "bathy_z": z.tolist()}


def _bathy_for(bounds: dict, max_depth: float) -> dict:
    """Bathymetry keys for the 3D payload. If the fetch fails: a flat floor just
    below the deepest dive, flagged `bathy_fallback` so it gets retried later."""
    try:
        b_lon, b_lat, b_z = _fetch_bathy_cached(
            round(bounds["min_lon"], 2), round(bounds["max_lon"], 2),
            round(bounds["min_lat"], 2), round(bounds["max_lat"], 2),
        )
        return {"bathy_lon": b_lon, "bathy_lat": b_lat, "bathy_z": b_z}
    except Exception:
        floor = -abs(max_depth) * 1.2
        return {
            "bathy_lon": [bounds["min_lon"], bounds["max_lon"]],
            "bathy_lat": [bounds["min_lat"], bounds["max_lat"]],
            "bathy_z": [[floor, floor], [floor, floor]],
            "bathy_fallback": True,
        }


_bathy_retry_at: dict = {}


def retry_bathy(payload: dict) -> bool:
    """Re-fetch bathymetry for a cached 3D payload stuck on the flat fallback.
    Patches `payload` in place; True if it now has real bathymetry. At most one
    attempt per 5 min per area."""
    b_lon = payload.get("bathy_lon") or []
    if not (payload.get("bathy_fallback") or len(b_lon) <= 2) or not payload.get("bounds"):
        return False
    key = json.dumps(payload["bounds"], sort_keys=True)
    now = time.time()
    if now - _bathy_retry_at.get(key, 0) < 300:
        return False
    _bathy_retry_at[key] = now
    elev = [e for e in (payload.get("elevation") or []) if e is not None and e == e]
    fresh = _bathy_for(payload["bounds"], -min(elev) if elev else 1000.0)
    payload.update(fresh)
    if fresh.get("bathy_fallback"):
        return False
    payload.pop("bathy_fallback", None)
    return True


# ---------- Core data path ----------

def _to_float_array(arr):
    if np.ma.isMaskedArray(arr):
        # Cast to float *before* filling so integer-typed masked arrays (e.g.
        # QC flags) can take NaN in their masked slots.
        return np.ma.filled(arr.astype(float), np.nan)
    return np.asarray(arr, dtype=float)


def _read_lat_lon_pres_temp(filepath):
    """Read LAT/LON/PRES/TEMP as plain float arrays, via the shared var-read cache."""
    needed = list(LAT_NAMES) + list(LON_NAMES) + ['PRES', 'TEMP']
    arrs = _read_named_arrays(filepath, needed)
    lat_name, lon_name = _resolve_latlon_names(arrs)
    if lat_name is None or lon_name is None:
        if not os.path.exists(filepath):
            raise FileNotFoundError("File not found")
        raise ValueError("No LATITUDE/LONGITUDE in this file")
    lat = arrs[lat_name]
    lon = arrs[lon_name]
    pres = arrs.get('PRES', np.zeros_like(lat))
    temp = arrs.get('TEMP')
    return lat, lon, pres, temp


def _read_named_arrays(filepath, names):
    """Read the given variables as float arrays, via the shared var-read cache
    (`plot_logic._read_vars_cached`) — same preloaded-disk/raw-file fallback
    the main plot pipeline uses, so it reads only
    the requested variables from disk (not every preloaded array) and reuses
    the result across repeated calls instead of re-reading every time.

    Missing variables are simply omitted from the result rather than raising,
    so callers can probe for optional fields (e.g. DAC, QC flags).
    """
    result = plot_logic._read_vars_cached(filepath, tuple(names)) or {}
    return {n: _to_float_array(arr) for n, arr in result.items()}


def _read_track_times(filepath):
    """Return the TIME coordinate as float epoch seconds (NaN where invalid).

    `_read_vars_cached` always goes through xarray (preloaded or raw-file
    fallback alike), which decodes CF time to ``datetime64[ns]``. That's
    collapsed to epoch seconds here so callers can do plain second-based
    arithmetic; the numeric branch below is a defensive fallback in case a
    caller ever hands this a raw (already-numeric) time array.
    """
    try:
        result = plot_logic._read_vars_cached(filepath, ('TIME', 'TIME_GPS')) or {}
    except Exception:
        return None
    raw = result.get('TIME')
    if raw is None:
        raw = result.get('TIME_GPS')
    if raw is None:
        return None

    if np.ma.isMaskedArray(raw):
        raw = np.ma.filled(raw.astype('float64') if not np.issubdtype(raw.dtype, np.datetime64) else raw, np.nan)
    raw = np.asarray(raw)
    if np.issubdtype(raw.dtype, np.datetime64):
        dt = raw.astype('datetime64[ns]')
        sec = dt.astype('int64').astype(float) / 1e9
        sec[np.isnat(dt)] = np.nan
        return sec
    # netCDF4 path: CF "seconds since 1970-01-01" — already epoch seconds.
    return raw.astype(float)


@functools.lru_cache(maxsize=32)
def get_dac_vectors(filepath):
    """Extract depth-averaged current (DAC) vectors along the glider track.

    Returns a list of ``{lat, lon, u, v, speed}`` dicts (velocities in m/s,
    eastward ``u`` / northward ``v``) ordered oldest-to-newest. One arrow per
    current estimate, placed at the GPS fix closest *in time* to it. These let
    the map draw a current arrow at each surfacing, which matters for NRT
    piloting in strong flow.

    The current estimate and the GPS fixes live on the same TIME axis but on
    different rows (current is logged mid-profile, fixes at the surface), so we
    match by nearest time rather than interpolating or snapping in space —
    spatial snapping mis-places vectors wherever the track loops back on
    itself. Vectors with no fix within ``DAC_MATCH_MAX_SEC`` are dropped, and
    the result is thinned to at most one per ``DAC_MIN_INTERVAL_SEC`` (always
    keeping the most recent) so dense records don't pile arrows on top of
    each other.
    """
    needed = list(LAT_NAMES) + list(LON_NAMES)
    for u_name, v_name, _ in DAC_VARIABLE_SETS:
        needed += [u_name, v_name, f"{u_name}_QC", f"{v_name}_QC"]

    try:
        arrs = _read_named_arrays(filepath, needed)
    except Exception:
        return []
    lat_name, lon_name = _resolve_latlon_names(arrs)
    if lat_name is None or lon_name is None:
        return []

    lat, lon = arrs[lat_name], arrs[lon_name]
    times = _read_track_times(filepath)

    u = v = u_qc = v_qc = None
    for u_name, v_name, scale in DAC_VARIABLE_SETS:
        cu, cv = arrs.get(u_name), arrs.get(v_name)
        if cu is not None and cv is not None and np.isfinite(cu).any() and np.isfinite(cv).any():
            u, v = cu * scale, cv * scale
            u_qc, v_qc = arrs.get(f"{u_name}_QC"), arrs.get(f"{v_name}_QC")
            break
    if u is None or times is None:
        return []

    n = min(len(lat), len(lon), len(u), len(v), len(times))
    lat, lon, u, v, times = lat[:n], lon[:n], u[:n], v[:n], times[:n]

    # Current estimates: finite, physically plausible, not QC-flagged bad.
    cur_ok = (
        np.isfinite(u) & np.isfinite(v) & np.isfinite(times)
        & (np.abs(u) < DAC_MAX_SPEED_MS) & (np.abs(v) < DAC_MAX_SPEED_MS)
    )
    # Drop only samples explicitly flagged bad (QC 3/4/9); keep 0 ("not
    # evaluated", which is how these files mark every DAC) and good flags.
    BAD_QC = np.array([3.0, 4.0, 9.0])
    for q in (u_qc, v_qc):
        if q is not None and len(q) >= n:
            cur_ok &= ~np.isin(q[:n], BAD_QC)

    # GPS fixes: finite, in range, timestamped.
    fix_ok = (
        np.isfinite(lat) & np.isfinite(lon) & np.isfinite(times)
        & (np.abs(lat) <= 90.0) & (np.abs(lon) <= 180.0)
    )

    cur_idx = np.where(cur_ok)[0]
    fix_idx = np.where(fix_ok)[0]
    if cur_idx.size == 0 or fix_idx.size == 0:
        return []

    # For each current, find the GPS fix nearest in time.
    ct = times[cur_idx]
    ft = times[fix_idx]
    order = np.argsort(ft)
    ft_sorted = ft[order]
    fix_sorted = fix_idx[order]
    pos = np.searchsorted(ft_sorted, ct)
    left = np.clip(pos - 1, 0, len(ft_sorted) - 1)
    right = np.clip(pos, 0, len(ft_sorted) - 1)
    take_left = np.abs(ft_sorted[left] - ct) <= np.abs(ft_sorted[right] - ct)
    best = np.where(take_left, left, right)
    dt = np.abs(ft_sorted[best] - ct)
    within = dt <= DAC_MATCH_MAX_SEC
    if not within.any():
        return []

    cur_idx = cur_idx[within]
    match_fix = fix_sorted[best[within]]
    ct = ct[within]
    out_lat, out_lon = lat[match_fix], lon[match_fix]
    out_u, out_v = u[cur_idx], v[cur_idx]

    # Thin to one per interval, walking newest -> oldest so the latest current
    # is always kept and older ones are only spaced out behind it.
    order_t = np.argsort(ct)
    ct_s = ct[order_t]
    keep = np.zeros(len(ct_s), dtype=bool)
    last_t = None
    for j in range(len(ct_s) - 1, -1, -1):
        if last_t is None or (last_t - ct_s[j]) >= DAC_MIN_INTERVAL_SEC:
            keep[j] = True
            last_t = ct_s[j]
    sel = order_t[keep]  # ascending time, oldest first

    if sel.size > DAC_MAX_VECTORS:        # safety cap, keep the most recent
        sel = sel[-DAC_MAX_VECTORS:]

    return [
        {
            "lat": round(float(out_lat[k]), 5),
            "lon": round(float(out_lon[k]), 5),
            "u": round(float(out_u[k]), 4),
            "v": round(float(out_v[k]), 4),
            "speed": round(float(np.hypot(out_u[k], out_v[k])), 4),
        }
        for k in sel
    ]


# Original-row indices of the points get_core_spatial_data() kept, keyed the
# same way as its lru_cache, so callers can pull extra per-point variables
# (e.g. pitch/roll for the 3D view) aligned with the cached track without
# widening that function's return signature.
_CORE_INDEX = {}


def get_core_spatial_index(filepath, max_points=MAX_POINTS):
    """Row indices (into the raw file arrays) of the cached spatial track."""
    get_core_spatial_data(filepath, max_points)
    return _CORE_INDEX.get((filepath, max_points))


@functools.lru_cache(maxsize=32)
def get_core_spatial_data(filepath, max_points=MAX_POINTS):
    """Read LAT/LON/PRES/TEMP, apply QC, subsample, and cache.

    Pipeline:
      1. Read raw arrays (from RAM if preloaded)
      2. Interpolate short coordinate gaps so brief NaN dropouts don't
         break the track path
      3. Basic validity: NaN, range bounds, common fill values
      4. Subsample to max_points *before* geographic outlier trimming so
         the trim runs on a small array regardless of file size — this is
         safe because we step through valid points only, not the full
         NaN-gapped raw array
      5. Trim geographic outliers (gap-split then keep largest group)
    """
    _report_spatial_stage("spatial QC: reading coordinates")
    lat, lon, pres, temp = _read_lat_lon_pres_temp(filepath)
    # Times (epoch seconds) drive the gap-break test in the outlier trim. Read
    # here so they ride through the same valid-mask + subsample as lat/lon and
    # stay row-aligned; if absent or misaligned the trim falls back to distance.
    try:
        times = _read_track_times(filepath)
    except Exception:
        times = None
    if times is not None and len(times) != len(lat):
        times = None

    _report_spatial_stage("spatial QC: interpolating coordinate gaps")
    lat = pd.Series(lat).interpolate(limit_direction='both').to_numpy()
    lon = pd.Series(lon).interpolate(limit_direction='both').to_numpy()

    _report_spatial_stage("spatial QC: range & fill-value filter")
    FILL_VALUES = np.array([-999.0, -9999.0, 999.0, 9999.0])
    valid = (
        ~np.isnan(lat) & ~np.isnan(lon) & ~np.isnan(pres)
        & (lat >= -90.0) & (lat <= 90.0)
        & (lon >= -180.0) & (lon <= 180.0)
        & ~np.isin(lat, FILL_VALUES)
        & ~np.isin(lon, FILL_VALUES)
    )

    if not valid.any():
        raise ValueError("No valid spatial data after QC filters")

    idx = np.flatnonzero(valid)
    lat = lat[valid]
    lon = lon[valid]
    pres = pres[valid]
    if temp is not None:
        temp = temp[valid]
    if times is not None:
        times = times[valid]

    # Subsample early so the geographic trim (and any future steps) work
    # on a small array. Step through valid points only so isolated GPS
    # fixes aren't accidentally skipped.
    _report_spatial_stage(f"spatial QC: subsampling {len(lat):,} valid fixes")
    if len(lat) > max_points:
        step = len(lat) // max_points
        idx = idx[::step]
        lat = lat[::step]
        lon = lon[::step]
        pres = pres[::step]
        if temp is not None:
            temp = temp[::step]
        if times is not None:
            times = times[::step]

    _report_spatial_stage("spatial QC: trimming position outliers")
    keep = _trim_position_outliers(lat, lon, times)
    idx = idx[keep]
    lat = lat[keep]
    lon = lon[keep]
    pres = pres[keep]
    if temp is not None:
        temp = temp[keep]
    if times is not None:
        times = times[keep]

    if len(lat) == 0:
        raise ValueError("No valid spatial data after position outlier trim")

    _CORE_INDEX[(filepath, max_points)] = idx
    return lat, lon, pres, temp, times


# ---------- API payloads ----------

def get_location_summary(filepath):
    try:
        lat, lon, _pres, _temp, _times = get_core_spatial_data(filepath)
    except Exception as e:
        return {"error": str(e)}
    return {
        "lat_min": float(np.min(lat)), "lat_max": float(np.max(lat)),
        "lon_min": float(np.min(lon)), "lon_max": float(np.max(lon)),
        "lat_center": float(np.mean(lat)), "lon_center": float(np.mean(lon)),
        "n_points": int(len(lat)),
    }


_TIME_EXTENT = {}   # (path, size, mtime) -> (first_iso, last_iso)


def get_time_extent_iso(filepath):
    """(first, last) ISO timestamps of the file's plottable TIME samples, or (None, None).

    Same validity rule as every plot (plot_logic._hard_time_valid_mask), so the
    shell can pre-align synced time axes to the range the plots will report.
    """
    import pandas as pd
    try:
        st = os.stat(filepath)
        key = (filepath, st.st_size, st.st_mtime)
    except OSError:
        return (None, None)
    hit = _TIME_EXTENT.get(key)
    if hit is not None:
        return hit
    pre = plot_logic._get_preloaded(filepath)
    time_arr = None
    if pre is not None:
        for k in ('TIME', 'TIME_GPS'):
            if k in pre:
                time_arr = pre[k]
                break
    if time_arr is None:
        try:
            with plot_logic.NETCDF_LOCK, Dataset(filepath, 'r') as nc:
                for k in ('TIME', 'TIME_GPS'):
                    if k in nc.variables:
                        time_arr = nc.variables[k][:]
                        break
        except Exception:
            return (None, None)
    out = (None, None)
    try:
        if time_arr is not None and len(time_arr):
            # Preloaded TIME is datetime64; raw netCDF TIME is CF epoch seconds. Naive UTC for the mask
            # (tz-aware -> datetime64 warns); the offset goes back on for the shell.
            arr = np.ma.filled(time_arr, np.nan) if np.ma.isMaskedArray(time_arr) else np.asarray(time_arr)
            ts = (pd.to_datetime(arr, errors='coerce') if np.issubdtype(arr.dtype, np.datetime64)
                  else pd.to_datetime(arr.astype(float), unit='s', errors='coerce'))
            ts = ts[plot_logic._hard_time_valid_mask(ts)]
            if len(ts):
                out = (ts.min().isoformat() + '+00:00', ts.max().isoformat() + '+00:00')
    except Exception:
        out = (None, None)
    if len(_TIME_EXTENT) > 512:
        _TIME_EXTENT.clear()
    _TIME_EXTENT[key] = out
    return out


def get_last_time_iso(filepath):
    """Return ISO timestamp of the most recent fix, or None.

    Used to flag Near-Real-Time (NRT) deployments — files whose final sample
    is recent enough that the glider is presumably still in the water.
    """
    import pandas as pd
    pre = plot_logic._get_preloaded(filepath)
    time_arr = None
    if pre is not None:
        for k in ('TIME', 'TIME_GPS'):
            if k in pre:
                time_arr = pre[k]
                break
    if time_arr is None:
        try:
            with plot_logic.NETCDF_LOCK, Dataset(filepath, 'r') as nc:
                for k in ('TIME', 'TIME_GPS'):
                    if k in nc.variables:
                        time_arr = nc.variables[k][:]
                        break
        except Exception:
            return None
    if time_arr is None or len(time_arr) == 0:
        return None
    try:
        ts = pd.to_datetime(time_arr, errors='coerce', utc=True)
        ts = ts[~pd.isna(ts)]
        if len(ts) == 0:
            return None
        return ts.max().isoformat()
    except Exception:
        return None


def get_track_endpoint(filepath):
    """Return the last QC'd lat/lon along the track (post-subsample)."""
    try:
        lat, lon, _pres, _temp, _times = get_core_spatial_data(filepath)
    except Exception:
        return None
    if len(lat) == 0:
        return None
    return {"last_lat": float(lat[-1]), "last_lon": float(lon[-1])}


def get_nearest_fix(filepath, time_ms):
    """Nearest in-time GPS fix to ``time_ms`` (epoch milliseconds).

    A clicked plot point carries a TIME but no position (LAT/LON are sparse —
    fixed only at the surface). We match the requested time to the closest
    sample that actually has a valid lat/lon, so the globe can pin where the
    glider was around that moment. Returns ``{lat, lon, time, dt_seconds}`` or
    an ``{error}`` dict.
    """
    try:
        lat, lon, _pres, _temp = _read_lat_lon_pres_temp(filepath)
        times = _read_track_times(filepath)  # epoch seconds, NaN where invalid
    except Exception as e:
        return {"error": str(e)}

    n = min(len(lat), len(lon), len(times))
    if n == 0:
        return {"error": "No position data"}
    lat, lon, times = lat[:n], lon[:n], times[:n]

    valid = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(times)
    idx = np.flatnonzero(valid)
    if idx.size == 0:
        return {"error": "No position fixes"}

    t_target = float(time_ms) / 1000.0
    j = idx[int(np.argmin(np.abs(times[idx] - t_target)))]
    return {
        "lat": float(lat[j]),
        "lon": float(lon[j]),
        "time": float(times[j]) * 1000.0,
        "dt_seconds": float(abs(times[j] - t_target)),
    }


def get_nearest_fix_by_coord(filepath, lat_q, lon_q):
    """Nearest in-space GPS fix to a clicked ``lat_q``/``lon_q`` position.

    The inverse of :func:`get_nearest_fix`: a globe click carries a POSITION but
    no time, so we match the clicked spot to the closest valid lat/lon sample and
    hand back the TIME there. That time can then drive the matching point on every
    open plot. Returns ``{lat, lon, time, dist_km}`` or an ``{error}`` dict.
    """
    try:
        lat, lon, _pres, _temp = _read_lat_lon_pres_temp(filepath)
        times = _read_track_times(filepath)  # epoch seconds, NaN where invalid
    except Exception as e:
        return {"error": str(e)}

    n = min(len(lat), len(lon), len(times))
    if n == 0:
        return {"error": "No position data"}
    lat, lon, times = lat[:n], lon[:n], times[:n]

    valid = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(times)
    idx = np.flatnonzero(valid)
    if idx.size == 0:
        return {"error": "No position fixes"}

    lat_q, lon_q = float(lat_q), float(lon_q)
    # Equirectangular approximation — cheap and plenty accurate over a glider's
    # local span for picking the nearest fix.
    cos_lat = np.cos(np.radians(lat_q))
    dx = (lon[idx] - lon_q) * cos_lat
    dy = lat[idx] - lat_q
    j = idx[int(np.argmin(dx * dx + dy * dy))]
    return {
        "lat": float(lat[j]),
        "lon": float(lon[j]),
        "time": float(times[j]) * 1000.0,
        "dist_km": float(np.hypot(
            (lon[j] - lon_q) * cos_lat, lat[j] - lat_q) * 111.195),
    }


def generate_map_image(filepath):
    t0 = time.time()
    try:
        lat, lon, _pres, _temp, _times = get_core_spatial_data(filepath)
    except Exception as e:
        return {"error": str(e)}
    t1 = time.time()
    path = [[round(float(y), 5), round(float(x), 5)] for y, x in zip(lat, lon)]
    dac = get_dac_vectors(filepath)
    t2 = time.time()
    return {
        "type": "native_data",
        "path": path,
        "dac": dac,
        "timings_seconds": {
            "data_load_qc": round(t1 - t0, 4),
            "json_formatting": round(t2 - t1, 4),
            "total": round(t2 - t0, 4),
        },
    }


# ---------- KMZ export ----------

_KMZ_SEGMENTS = 150
_KMZ_YELLOW = "ff00d5ff"  # KML aabbggrr — solid yellow, matches the app's track


def _kml_time(epoch_s):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(epoch_s), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_kmz(filepath, name):
    """Build a KMZ (zipped KML) of the glider's surface track for Google Earth.

    Uses the same QC'd path as the map view: a solid yellow track plus
    Start/End pins. When track times are available the line is split into
    segments carrying begin-only TimeSpans, so Google Earth's time slider
    replays the deployment (the track grows as time advances); without times
    it's a single plain LineString. Raises on files with no valid positions —
    the endpoint turns that into an HTTP error.
    """
    lat, lon, _pres, _temp, times = get_core_spatial_data(filepath)
    n = len(lat)
    title = _xml_escape(name)

    if times is not None and np.isfinite(times).sum() >= 2:
        finite = times[np.isfinite(times)]
        t0, t1 = float(finite.min()), float(finite.max())
        time_ok = t1 > t0
    else:
        time_ok = False

    def coord(i):
        return f"{float(lon[i]):.5f},{float(lat[i]):.5f},0"

    if time_ok:
        # Contiguous buckets, one Placemark each, sharing their boundary point
        # so the line stays continuous. Begin-only TimeSpan: each segment
        # appears at its start time and stays, so playback draws the track
        # progressively.
        n_seg = min(_KMZ_SEGMENTS, max(n - 1, 1))
        bounds = np.linspace(0, n - 1, n_seg + 1).round().astype(int)
        parts = []
        for k in range(n_seg):
            i0, i1 = int(bounds[k]), int(bounds[k + 1])
            if i1 <= i0:
                continue
            seg_t = times[i0:i1 + 1]
            seg_t = seg_t[np.isfinite(seg_t)]
            timespan = f"<TimeSpan><begin>{_kml_time(seg_t.min())}</begin></TimeSpan>" if len(seg_t) else ""
            coords = "\n".join(coord(i) for i in range(i0, i1 + 1))
            parts.append(
                "      <Placemark>"
                f"{timespan}"
                "<styleUrl>#track</styleUrl>"
                "<LineString><tessellate>1</tessellate><coordinates>\n"
                f"{coords}\n"
                "</coordinates></LineString></Placemark>"
            )
        track_kml = "\n".join(parts)
    else:
        all_coords = "\n".join(coord(i) for i in range(n))
        track_kml = (
            "      <Placemark><styleUrl>#track</styleUrl>"
            "<LineString><tessellate>1</tessellate><coordinates>\n"
            f"{all_coords}\n"
            "</coordinates></LineString></Placemark>"
        )

    desc_lines = [f"{n:,} surface fixes"]
    if time_ok:
        desc_lines.append(f"{_kml_time(t0)} → {_kml_time(t1)}")
    description = _xml_escape(" | ".join(desc_lines))

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{title}</name>
    <description>{description}</description>
    <Style id="track">
      <LineStyle><color>{_KMZ_YELLOW}</color><width>3</width></LineStyle>
    </Style>
    <Folder>
      <name>Track</name>
      <open>0</open>
{track_kml}
    </Folder>
    <Folder>
      <name>Markers</name>
      <Placemark>
        <name>Start</name>
        <Point><coordinates>{coord(0)}</coordinates></Point>
      </Placemark>
      <Placemark>
        <name>End</name>
        <Point><coordinates>{coord(n - 1)}</coordinates></Point>
      </Placemark>
    </Folder>
  </Document>
</kml>
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml)
    return buf.getvalue()


def generate_3d_data(filepath):
    try:
        lat, lon, pres, temp, times = get_core_spatial_data(filepath, MAX_POINTS_3D)
    except Exception as e:
        return {"error": f"Internal error: {e}"}

    # Epoch-ms per track point (None where missing) - drives the position
    # slider's timestamp readout in the 3D view. Kept numeric so the frontend
    # never has to parse a date string (see the timezone note in CLAUDE.md).
    time_ms = None
    if times is not None and len(times) == len(lat):
        time_ms = [None if np.isnan(t) else float(t) * 1000.0 for t in times]

    # Vehicle attitude (degrees, None where missing) so the 3D view can pose
    # the model with the measured pitch/roll instead of the track tangent.
    # Files vary in naming; OG1 uses PITCH/ROLL, others GLIDER_PITCH/GLIDER_ROLL.
    pitch = roll = heading = None
    try:
        idx = get_core_spatial_index(filepath, MAX_POINTS_3D)
        if idx is not None and len(idx) == len(lat):
            arrs = _read_named_arrays(filepath, ['GLIDER_PITCH', 'PITCH', 'GLIDER_ROLL', 'ROLL',
                                                 'GLIDER_HEADING', 'HEADING'])
            units = plot_logic._get_var_units(filepath)

            def _first(*names):
                for nm in names:
                    a = arrs.get(nm)
                    if a is not None and len(a) > idx.max() and np.isfinite(a).any():
                        return nm, a.astype(float)
                return None, None

            # Units attributes can't be trusted: Slocum-derived files often say
            # "deg" while holding radians. A glider's pitch is tens of degrees,
            # so a file whose |pitch| never exceeds ~pi/2 is in radians - and
            # roll/heading come from the same sensor block, so they follow it.
            p_name, p_raw = _first('GLIDER_PITCH', 'PITCH')
            is_rad = False
            if p_raw is not None:
                is_rad = ('rad' in str(units.get(p_name, '')).lower()
                          or np.nanpercentile(np.abs(p_raw), 99) < 1.6)
                p_raw_deg = np.degrees(p_raw) if is_rad else p_raw

            # Real-time files carry attitude only every few minutes, with gaps
            # of hours: interpolating across those smears dives into climbs.
            # A track point trusts the sensors only when it sits between two
            # samples that are both within ATT_MAX_GAP_S of it.
            ATT_MAX_GAP_S = 600.0
            tfull = _read_track_times(filepath)
            have_t = (tfull is not None and len(tfull) > idx.max()
                      and times is not None and len(times) == len(idx))

            def _attitude(*names, circular=False):
                nm, a = _first(*names)
                if a is None:
                    return None, None
                if is_rad:
                    a = np.degrees(a)
                a[np.abs(a) > 360] = np.nan
                ok = np.where(np.isfinite(a))[0]
                if len(ok) < 2:
                    return None, None
                if circular:
                    r = np.radians(a[ok])
                    v = np.degrees(np.arctan2(np.interp(idx, ok, np.sin(r)), np.interp(idx, ok, np.cos(r)))) % 360
                else:
                    v = np.interp(idx, ok, a[ok])
                trusted = np.ones(len(idx), dtype=bool)
                if have_t:
                    k = np.searchsorted(ok, idx)               # next sample at/after each track row
                    lo = ok[np.clip(k - 1, 0, len(ok) - 1)]
                    hi = ok[np.clip(k, 0, len(ok) - 1)]
                    with np.errstate(invalid='ignore'):
                        trusted = ((np.abs(times - tfull[lo]) <= ATT_MAX_GAP_S)
                                   & (np.abs(tfull[hi] - times) <= ATT_MAX_GAP_S))
                return v, trusted

            def _out(v):
                return [None if not np.isfinite(x) else round(float(x), 2) for x in v]

            pv, pt = _attitude('GLIDER_PITCH', 'PITCH')
            if pv is not None:
                # Away from trusted samples: the file's typical dive/climb angle,
                # signed by the vertical speed of the track (level when hovering).
                typ_down = np.nanmedian(np.abs(p_raw_deg[p_raw_deg < -5])) if (p_raw_deg < -5).any() else 20.0
                typ_up = np.nanmedian(np.abs(p_raw_deg[p_raw_deg > 5])) if (p_raw_deg > 5).any() else 20.0
                if have_t:
                    with np.errstate(invalid='ignore', divide='ignore'):
                        w = np.gradient(np.asarray(pres, dtype=float), times)   # m/s, +ve sinking
                else:
                    w = np.gradient(np.asarray(pres, dtype=float)) * np.inf
                w = np.nan_to_num(w, nan=0.0, posinf=1.0, neginf=-1.0)
                est = np.where(w > 0.02, -typ_down, np.where(w < -0.02, typ_up, 0.0))
                pitch = _out(np.where(pt, pv, est))
            rv, rt = _attitude('GLIDER_ROLL', 'ROLL')
            if rv is not None:
                roll = _out(np.where(rt, rv, 0.0))
            hv, ht = _attitude('GLIDER_HEADING', 'HEADING', circular=True)
            if hv is not None:
                # Heading is held between surfacings, so bridge compass gaps (switching to track direction made
                # the model snap) and smooth with a circular mean.
                r = np.radians(hv)
                win = max(3, min(41, (len(hv) // 500) | 1))
                ker = np.hanning(win + 2)[1:-1]
                ker /= ker.sum()
                sn = np.convolve(np.pad(np.sin(r), win // 2, mode='edge'), ker, mode='valid')
                cs = np.convolve(np.pad(np.cos(r), win // 2, mode='edge'), ker, mode='valid')
                heading = _out(np.degrees(np.arctan2(sn, cs)) % 360)
    except Exception:
        pitch = roll = heading = None

    min_lon, max_lon = float(np.min(lon)), float(np.max(lon))
    min_lat, max_lat = float(np.min(lat)), float(np.max(lat))
    lon_pad = (max_lon - min_lon) * 0.15 or 0.1
    lat_pad = (max_lat - min_lat) * 0.15 or 0.1
    bounds = {
        "min_lon": min_lon - lon_pad, "max_lon": max_lon + lon_pad,
        "min_lat": min_lat - lat_pad, "max_lat": max_lat + lat_pad,
    }

    payload_bathy = _bathy_for(bounds, float(np.nanmax(pres)) if len(pres) > 0 else 1000.0)

    return {
        "lon": lon.tolist(),
        "lat": lat.tolist(),
        "elevation": (-pres).tolist(),
        "temp": [None if np.isnan(t) else float(t) for t in temp] if temp is not None else None,
        "time_ms": time_ms,
        "pitch": pitch,
        "roll": roll,
        "heading": heading,
        **payload_bathy,
        "bounds": bounds,
    }


# ---------- Track colour (3D view / missions) ----------

TRACK_COLOUR_MAX_GAP = 40      # track points (of ~20k per file) a missing colour value may be interpolated across

def _track_colour_presets(filepath):
    """Presets usable as a 3D track colour for this file: time-section presets with a continuous palette whose
    colour variable is present. -> [(preset key, preset, variable name)]"""
    cfg = presets_logic.load()
    names = set(plot_logic._get_var_names(filepath) or [])
    time_axis = set(presets_logic._candidates(cfg, "time"))
    out = []
    for key, p in cfg.get("presets", {}).items():
        if not set(presets_logic._candidates(cfg, p.get("x"))) & time_axis:
            continue
        for cand in presets_logic._candidates(cfg, p.get("c")):
            var = f"{cand}_ADJUSTED" if f"{cand}_ADJUSTED" in names else cand
            if var in names:
                out.append((key, p, var))
                break
    return out


def track_colour_options(filepath) -> list:
    return [{"key": k, "label": p.get("label", k), "var": v, "cmap": p.get("cmap")} for k, p, v in _track_colour_presets(filepath)]


_track_colour_cache = {}       # (path, size, mtime, var) -> result sans preset fields; small, newest kept
_TRACK_COLOUR_CACHE_MAX = 24


def track_colour(filepath, preset_key=None, var=None, cmap=None) -> dict:
    """One value per 3D-track point for a colour variable (a preset's, or any numeric `var` so the 3D view can
    follow a plot): the mean of the raw samples each track point stands for (sparse sensors would otherwise
    mostly miss the sampled rows). Limits are the 2nd-98th percentiles."""
    if preset_key:
        hit = next(((p, v) for k, p, v in _track_colour_presets(filepath) if k == preset_key), None)
        if not hit:
            return {"error": "Not available for this file"}
        preset, var = hit
    else:
        if not var or var not in set(plot_logic._get_var_names(filepath) or []):
            return {"error": "Not available for this file"}
        preset = {"label": var, "cmap": cmap}
    head = {"preset": preset_key or "", "label": preset.get("label", var), "var": var, "cmap": cmap or preset.get("cmap")}
    discrete = str(head["cmap"] or "").startswith("discrete")
    try:
        st = os.stat(filepath)
        ckey = (str(filepath), st.st_size, st.st_mtime, var, discrete)
    except OSError:
        ckey = None
    if ckey in _track_colour_cache:
        return {**_track_colour_cache[ckey], **head}
    body = _track_colour_values(filepath, var, discrete)
    if ckey and "error" not in body:
        while len(_track_colour_cache) >= _TRACK_COLOUR_CACHE_MAX:
            _track_colour_cache.pop(next(iter(_track_colour_cache)))
        _track_colour_cache[ckey] = body
    return {**body, **head}


def _track_colour_values(filepath, var, discrete=False) -> dict:
    idx = get_core_spatial_index(filepath, MAX_POINTS_3D)
    if idx is None or not len(idx):
        return {"error": "Not available for this file"}
    raw = _read_named_arrays(filepath, [var]).get(var)
    if raw is None or raw.dtype.kind not in "fiu" or len(raw) <= int(idx.max()):
        return {"error": "Not available for this file"}
    idx = np.asarray(idx, dtype=np.int64)
    if discrete:                   # integer flags (0-9): the point's own sample, never a mean
        vals = raw[idx].astype(float)
        vals[~((vals >= 0) & (vals <= 9))] = np.nan
        return {"discrete": True, "units": "", "cmin": 0.0, "cmax": 9.0,
                "values": [None if not np.isfinite(v) else int(round(v)) for v in vals]}
    edges = np.concatenate(([0], (idx[:-1] + idx[1:]) // 2 + 1))          # each point owns the rows nearest to it
    ok = np.isfinite(raw)
    sums = np.add.reduceat(np.where(ok, raw, 0.0), edges)
    counts = np.add.reduceat(ok.astype(np.int64), edges)
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = np.where(counts > 0, sums / counts, np.nan)
    # Sparse sensors leave most track points empty; bridge gaps up to TRACK_COLOUR_MAX_GAP points (linear), so the
    # line reads as one colour ramp rather than flickering to "no data" between every sample. Long gaps stay empty.
    good = np.flatnonzero(np.isfinite(vals))
    if 1 < good.size < vals.size:
        pos = np.arange(vals.size)
        nxt = np.searchsorted(good, pos, side="left").clip(0, good.size - 1)
        prv = (np.searchsorted(good, pos, side="right") - 1).clip(0, good.size - 1)
        bridge = ~np.isfinite(vals) & (good[nxt] - good[prv] <= TRACK_COLOUR_MAX_GAP) & (good[nxt] > pos) & (good[prv] < pos)
        vals[bridge] = np.interp(pos[bridge], good, vals[good])
    finite = vals[np.isfinite(vals)]
    lo, hi = (np.percentile(finite, [2, 98]) if finite.size else (0.0, 1.0))
    return {"units": (plot_logic._get_var_units(filepath) or {}).get(var, ""),
            "cmin": float(lo), "cmax": float(hi if hi > lo else lo + 1e-9),
            "values": [None if not np.isfinite(v) else float(f"{v:.5g}") for v in vals]}
