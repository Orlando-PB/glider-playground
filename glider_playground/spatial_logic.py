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
    """Drop fixes far from the bulk of the track.

    Splits the track into groups whenever there is a spatial gap
    larger than GEO_GAP_THRESHOLD_KM. Keeps the largest group
    that meets the GEO_GROUP_MIN_POINTS requirement.
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

    largest_group = max(groups, key=len)

    if len(largest_group) >= GEO_GROUP_MIN_POINTS:
        valid[largest_group] = True
    else:
        valid[largest_group] = True

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
        return arr.filled(np.nan).astype(float, copy=False)
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


def generate_map_image(filepath):
    t0 = time.time()
    try:
        lat, lon, _pres, _temp = get_core_spatial_data(filepath)
    except Exception as e:
        return {"error": str(e)}
    t1 = time.time()
    path = [[round(float(y), 5), round(float(x), 5)] for y, x in zip(lat, lon)]
    t2 = time.time()
    return {
        "type": "native_data",
        "path": path,
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
