"""Copernicus Marine surface overlays — fetch + lightweight session cache.

Generalises the original CHL-a overlay to any registered variable (chlorophyll,
temperature, salinity, oxygen, pH, phytoplankton biomass). Each overlay maps to
one or more Copernicus dataset ids and a variable name; the satellite ocean-colour
product is L4 (2D), the rest are 3D model analysis/forecast products from which we
take the surface level. The map view draws the returned point grid as coloured
cells on the globe.
"""

import logging
import re
import time
from datetime import datetime, timedelta

import numpy as np

logger = logging.getLogger(__name__)

# Satellite ocean-colour: near-real-time (last ~1y) then multi-year reprocessed.
_DS_CHL_NRT = "cmems_obs-oc_glo_bgc-plankton_nrt_l4-gapfree-multi-4km_P1D"
_DS_CHL_MY = "cmems_obs-oc_glo_bgc-plankton_my_l4-gapfree-multi-4km_P1D"

# Registry of available overlays. `surface` marks the 3D model products whose
# top depth level we extract. Order here is the order shown in the map legend.
OVERLAYS: dict[str, dict] = {
    "chla":     {"datasets": [_DS_CHL_NRT, _DS_CHL_MY], "variable": "CHL",    "surface": False},
    "temp":     {"datasets": ["cmems_mod_glo_phy-thetao_anfc_0.083deg_P1D-m"], "variable": "thetao", "surface": True},
    "salinity": {"datasets": ["cmems_mod_glo_phy-so_anfc_0.083deg_P1D-m"],     "variable": "so",     "surface": True},
    "o2":       {"datasets": ["cmems_mod_glo_bgc-bio_anfc_0.25deg_P1D-m"],     "variable": "o2",     "surface": True},
    "ph":       {"datasets": ["cmems_mod_glo_bgc-car_anfc_0.25deg_P1D-m"],     "variable": "ph",     "surface": True},
    "biomass":  {"datasets": ["cmems_mod_glo_bgc-pft_anfc_0.25deg_P1D-m"],     "variable": "phyc",   "surface": True},
}

# Session-level LRU dict: key → result; avoids re-fetching on repeat toggle.
_CACHE: dict = {}
_MAX_CACHE = 24

# Keep the cell grid under this per side so the globe stays responsive. High
# enough that the model products (≤0.25°) and most of the satellite grid render
# at their native resolution; the merged-mesh overlay handles this easily.
_MAX_CELLS_PER_SIDE = 400


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

    try:
        import copernicusmarine
    except ImportError:
        return {
            "error": "copernicusmarine package is not installed",
            "hint": "Run: pip install copernicusmarine",
        }

    pad = 4.0  # degrees of context around the glider bbox (~doubles the area vs 2°)
    min_lat = max(-90.0, lat_min - pad)
    max_lat = min(90.0, lat_max + pad)
    min_lon = max(-180.0, lon_min - pad)
    max_lon = min(180.0, lon_max + pad)

    if target_date:
        date_str = target_date[:10]
    else:
        date_str = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")

    key = _cache_key(var, min_lat, max_lat, min_lon, max_lon, date_str)
    if key in _CACHE:
        logger.info("Overlay %s cache hit for %s", var, date_str)
        print(f"[{var}] Cache hit for {date_str}", flush=True)
        return _CACHE[key]

    print(f"[{var}] Fetching for date={date_str} bbox=({min_lat:.1f},{min_lon:.1f})-({max_lat:.1f},{max_lon:.1f})", flush=True)
    t0 = time.time()
    result = _fetch(copernicusmarine, spec, min_lat, max_lat, min_lon, max_lon, date_str)
    elapsed = time.time() - t0
    if "error" in result:
        print(f"[{var}] Fetch failed in {elapsed:.1f}s: {result['error']}", flush=True)
    else:
        print(f"[{var}] Fetch OK in {elapsed:.1f}s — {len(result['points'])} points, date={result['date']}", flush=True)
        if len(_CACHE) >= _MAX_CACHE:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = result

    return result


# ---------- internals ----------

def _fetch(cm, spec, min_lat, max_lat, min_lon, max_lon, date_str):
    """Try each candidate dataset; cap the date to the dataset's max on overrun."""
    last_err = None
    for dataset_id in spec["datasets"]:
        try:
            return _open_and_extract(cm, dataset_id, spec, min_lat, max_lat, min_lon, max_lon, date_str)
        except Exception as exc:
            msg = str(exc)
            logger.warning("Overlay fetch error (dataset=%s): %s", dataset_id, msg)

            if _is_auth_error(msg):
                return {
                    "error": "Copernicus Marine authentication failed",
                    "hint": "Run 'copernicusmarine login' in your terminal, then restart Glider Playground",
                }

            # If our date is beyond the dataset's range, retry at its actual max.
            if _is_bounds_error(msg):
                capped = _parse_max_date(msg)
                if capped and capped != date_str:
                    logger.info("Date %s exceeds range — retrying with %s", date_str, capped)
                    try:
                        return _open_and_extract(cm, dataset_id, spec, min_lat, max_lat,
                                                 min_lon, max_lon, capped)
                    except Exception as exc2:
                        logger.warning("Overlay retry error (dataset=%s, date=%s): %s",
                                       dataset_id, capped, exc2)
                        last_err = str(exc2)
                        continue

            last_err = msg

    return {
        "error": f"Could not retrieve data: {last_err}",
        "hint": "Ensure you are logged in: run 'copernicusmarine login' in your terminal",
    }


def _open_and_extract(cm, dataset_id, spec, min_lat, max_lat, min_lon, max_lon, date_str):
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
    ds = cm.open_dataset(**kwargs)
    print(f"[{spec['variable']}] open_dataset done in {time.time()-t0:.1f}s", flush=True)
    return _extract(ds, spec["variable"], date_str)


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


def _extract(ds, variable: str, date_str: str) -> dict:
    """Flatten the (surface) field into a list of [lat, lng, value] cells.

    Subsamples by an adaptive stride so the payload stays small and the globe's
    overlay mesh is fast to build — at most _MAX_CELLS_PER_SIDE cells per axis.
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
    p10 = float(np.percentile(flat, 10))
    p90 = float(np.percentile(flat, 90))

    points = [
        [round(float(la), 4), round(float(lo), 4), round(float(v), 5)]
        for la, lo, v in zip(lat_grid[mask], lon_grid[mask], flat)
    ]
    logger.info("Overlay %s: %d points for %s (p10=%.3f p90=%.3f, stride=%d)",
                variable, len(points), date_str, p10, p90, stride)

    return {"points": points, "date": date_str, "p10": p10, "p90": p90,
            "half_deg": half_deg, "units": units}
