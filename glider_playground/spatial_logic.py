"""Spatial QC, map path, and 3D view payloads.

The map and 3D view both share the same QC'd lat/lon/pres/temp arrays
produced by `get_core_spatial_data`. Results are cached per-file so
switching back and forth is free.
"""

import functools
import io
import os
import time

import numpy as np
import pandas as pd
import requests
from netCDF4 import Dataset

from . import plot_logic

MAX_POINTS = 5000
BATHY_RESOLUTION = 40
GEO_GAP_THRESHOLD_KM = 20.0
GEO_GROUP_MIN_POINTS = 100

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

def _trim_position_outliers(lat, lon):
    """Drop isolated stray fixes while keeping the whole real track.

    Splits the track into groups wherever there is a spatial gap larger than
    GEO_GAP_THRESHOLD_KM, then keeps *every* group with at least
    GEO_GROUP_MIN_POINTS fixes. A glider legitimately leaves big gaps behind
    (a long transit dive, a comms outage), so keeping only the single largest
    group used to discard whole later legs of the deployment. Only genuinely
    tiny clusters — a lone bad GPS fix flung far from the track — are dropped.
    """
    n = len(lat)
    valid = np.zeros(n, dtype=bool)

    if n < 2:
        return np.ones(n, dtype=bool)

    cos_lat = np.cos(np.deg2rad(lat[:-1]))
    dy = (lat[1:] - lat[:-1]) * 110.574
    dx = (lon[1:] - lon[:-1]) * 111.320 * cos_lat
    dist = np.hypot(dx, dy)

    gap_indices = np.where(dist > GEO_GAP_THRESHOLD_KM)[0] + 1
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
    url = (
        "https://coastwatch.pfeg.noaa.gov/erddap/griddap/etopo180.csv"
        f"?altitude[({min_lat:.4f}):({max_lat:.4f})][({min_lon:.4f}):({max_lon:.4f})]"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text), skiprows=[1]).dropna(subset=["altitude"])
    lats = np.sort(df["latitude"].unique())
    lons = np.sort(df["longitude"].unique())

    lat_step = max(1, len(lats) // BATHY_RESOLUTION)
    lon_step = max(1, len(lons) // BATHY_RESOLUTION)
    lats = lats[::lat_step]
    lons = lons[::lon_step]

    df = df[df["latitude"].isin(lats) & df["longitude"].isin(lons)]
    pivot = df.pivot(index="latitude", columns="longitude", values="altitude") \
              .reindex(index=lats, columns=lons)

    return lons.tolist(), lats.tolist(), pivot.values.tolist()


# ---------- Core data path ----------

def _to_float_array(arr):
    if np.ma.isMaskedArray(arr):
        # Cast to float *before* filling so integer-typed masked arrays (e.g.
        # QC flags) can take NaN in their masked slots.
        return np.ma.filled(arr.astype(float), np.nan)
    return np.asarray(arr, dtype=float)


def _read_lat_lon_pres_temp(filepath):
    """Read LAT/LON/PRES/TEMP as plain float arrays, preferring preloaded RAM."""
    pre = plot_logic._get_preloaded(filepath)
    if pre is not None:
        if 'LATITUDE' not in pre or 'LONGITUDE' not in pre:
            raise ValueError("No LATITUDE/LONGITUDE in this file")
        lat = pre['LATITUDE']
        lon = pre['LONGITUDE']
        pres = pre['PRES'] if 'PRES' in pre else np.zeros_like(lat)
        temp = pre.get('TEMP')
    else:
        if not os.path.exists(filepath):
            raise FileNotFoundError("File not found")
        with Dataset(filepath, 'r') as nc:
            if 'LATITUDE' not in nc.variables or 'LONGITUDE' not in nc.variables:
                raise ValueError("No LATITUDE/LONGITUDE in this file")
            lat = nc.variables['LATITUDE'][:]
            lon = nc.variables['LONGITUDE'][:]
            pres = nc.variables['PRES'][:] if 'PRES' in nc.variables else np.zeros_like(lat)
            temp = nc.variables['TEMP'][:] if 'TEMP' in nc.variables else None

    lat = _to_float_array(lat)
    lon = _to_float_array(lon)
    pres = _to_float_array(pres)
    temp = _to_float_array(temp) if temp is not None else None
    return lat, lon, pres, temp


def _read_named_arrays(filepath, names):
    """Read the given variables as float arrays, preferring preloaded RAM/disk.

    Missing variables are simply omitted from the result rather than raising,
    so callers can probe for optional fields (e.g. DAC, QC flags).
    """
    pre = plot_logic._get_preloaded(filepath)
    out = {}
    if pre is not None:
        for n in names:
            if n in pre:
                out[n] = _to_float_array(pre[n])
        return out
    if not os.path.exists(filepath):
        raise FileNotFoundError("File not found")
    with Dataset(filepath, 'r') as nc:
        for n in names:
            if n in nc.variables:
                out[n] = _to_float_array(nc.variables[n][:])
    return out


def _read_track_times(filepath):
    """Return the TIME coordinate as float epoch seconds (NaN where invalid).

    Two storage forms must be handled: the RAM/disk preload goes through
    xarray which decodes CF time to ``datetime64[ns]``, while the netCDF4
    fallback yields the raw numeric ``seconds since 1970``. Both collapse to
    epoch seconds here so callers can do plain second-based arithmetic.
    """
    pre = plot_logic._get_preloaded(filepath)
    raw = None
    if pre is not None:
        for k in ('TIME', 'TIME_GPS'):
            if k in pre:
                raw = pre[k]
                break
    else:
        try:
            with Dataset(filepath, 'r') as nc:
                for k in ('TIME', 'TIME_GPS'):
                    if k in nc.variables:
                        raw = nc.variables[k][:]
                        break
        except Exception:
            return None
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
    needed = ['LATITUDE', 'LONGITUDE']
    for u_name, v_name, _ in DAC_VARIABLE_SETS:
        needed += [u_name, v_name, f"{u_name}_QC", f"{v_name}_QC"]

    try:
        arrs = _read_named_arrays(filepath, needed)
    except Exception:
        return []
    if 'LATITUDE' not in arrs or 'LONGITUDE' not in arrs:
        return []

    lat, lon = arrs['LATITUDE'], arrs['LONGITUDE']
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

    lat = lat[valid]
    lon = lon[valid]
    pres = pres[valid]
    if temp is not None:
        temp = temp[valid]

    # Subsample early so the geographic trim (and any future steps) work
    # on a small array. Step through valid points only so isolated GPS
    # fixes aren't accidentally skipped.
    _report_spatial_stage(f"spatial QC: subsampling {len(lat):,} valid fixes")
    if len(lat) > max_points:
        step = len(lat) // max_points
        lat = lat[::step]
        lon = lon[::step]
        pres = pres[::step]
        if temp is not None:
            temp = temp[::step]

    _report_spatial_stage("spatial QC: trimming position outliers")
    keep = _trim_position_outliers(lat, lon)
    lat = lat[keep]
    lon = lon[keep]
    pres = pres[keep]
    if temp is not None:
        temp = temp[keep]

    if len(lat) == 0:
        raise ValueError("No valid spatial data after position outlier trim")

    return lat, lon, pres, temp


# ---------- API payloads ----------

def get_location_summary(filepath):
    try:
        lat, lon, _pres, _temp = get_core_spatial_data(filepath)
    except Exception as e:
        return {"error": str(e)}
    return {
        "lat_min": float(np.min(lat)), "lat_max": float(np.max(lat)),
        "lon_min": float(np.min(lon)), "lon_max": float(np.max(lon)),
        "lat_center": float(np.mean(lat)), "lon_center": float(np.mean(lon)),
        "n_points": int(len(lat)),
    }


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
            with Dataset(filepath, 'r') as nc:
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
        lat, lon, _pres, _temp = get_core_spatial_data(filepath)
    except Exception:
        return None
    if len(lat) == 0:
        return None
    return {"last_lat": float(lat[-1]), "last_lon": float(lon[-1])}


def generate_map_image(filepath):
    t0 = time.time()
    try:
        lat, lon, _pres, _temp = get_core_spatial_data(filepath)
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


def generate_3d_data(filepath):
    try:
        lat, lon, pres, temp = get_core_spatial_data(filepath)
    except Exception as e:
        return {"error": f"Internal error: {e}"}

    min_lon, max_lon = float(np.min(lon)), float(np.max(lon))
    min_lat, max_lat = float(np.min(lat)), float(np.max(lat))
    lon_pad = (max_lon - min_lon) * 0.15 or 0.1
    lat_pad = (max_lat - min_lat) * 0.15 or 0.1
    bounds = {
        "min_lon": min_lon - lon_pad, "max_lon": max_lon + lon_pad,
        "min_lat": min_lat - lat_pad, "max_lat": max_lat + lat_pad,
    }

    try:
        b_lon, b_lat, b_z = _fetch_bathy_cached(
            round(bounds["min_lon"], 2), round(bounds["max_lon"], 2),
            round(bounds["min_lat"], 2), round(bounds["max_lat"], 2),
        )
    except Exception:
        # Network down or bathy unavailable — flat floor falls back to glider depth.
        b_lon = [bounds["min_lon"], bounds["max_lon"]]
        b_lat = [bounds["min_lat"], bounds["max_lat"]]
        floor = float(np.nanmin(pres) * 1.2) if len(pres) > 0 else 1000.0
        b_z = [[floor, floor], [floor, floor]]

    return {
        "lon": lon.tolist(),
        "lat": lat.tolist(),
        "elevation": (-pres).tolist(),
        "temp": [None if np.isnan(t) else float(t) for t in temp] if temp is not None else None,
        "bathy_lon": b_lon,
        "bathy_lat": b_lat,
        "bathy_z": b_z,
        "bounds": bounds,
    }
