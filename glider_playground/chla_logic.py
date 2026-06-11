"""Copernicus Marine CHLA overlay — fetch and lightweight session cache.

Fetches ocean-colour chlorophyll-a (CHL) for a glider's bounding box from the
Copernicus Marine ocean-colour product and returns a flat point grid the map
view can draw as coloured cells on the globe. Results are cached per session so
repeated toggling does not re-hit the network.
"""

import logging
import re
import time
from datetime import datetime, timedelta

import numpy as np

logger = logging.getLogger(__name__)

# Near-real-time (last ~1 year) and multi-year reprocessed (1997–present) datasets
_DS_NRT = "cmems_obs-oc_glo_bgc-plankton_nrt_l4-gapfree-multi-4km_P1D"
_DS_MY = "cmems_obs-oc_glo_bgc-plankton_my_l4-gapfree-multi-4km_P1D"

# Session-level LRU dict: key → result; avoids re-fetching on repeat toggle
_CACHE: dict = {}
_MAX_CACHE = 12

# Cap the cell grid so the globe's polygon layer stays responsive. The native
# product is ~4 km; we subsample to keep the number of drawn cells per side
# under this, picking the stride from the actual grid shape (see _extract).
_MAX_CELLS_PER_SIDE = 80


def _cache_key(lat_min, lat_max, lon_min, lon_max, date_str):
    # Round to 0.5° so small pan differences hit the same entry
    return (round(lat_min * 2) / 2, round(lat_max * 2) / 2,
            round(lon_min * 2) / 2, round(lon_max * 2) / 2, date_str)


def fetch_chla_overlay(
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    target_date: str | None = None,
) -> dict:
    """
    Return CHLA point grid for the bounding box.

    On success: {"points": [[lat, lng, chl], ...], "date": "YYYY-MM-DD",
                 "p10": float, "p90": float, "half_deg": float}
    On failure: {"error": str, "hint": str}
    """
    try:
        import copernicusmarine
    except ImportError:
        return {
            "error": "copernicusmarine package is not installed",
            "hint": "Run: pip install copernicusmarine",
        }

    pad = 2.0
    min_lat = max(-90.0, lat_min - pad)
    max_lat = min(90.0, lat_max + pad)
    min_lon = max(-180.0, lon_min - pad)
    max_lon = min(180.0, lon_max + pad)

    if target_date:
        date_str = target_date[:10]
    else:
        # NRT product is typically available with a ~2-day lag
        date_str = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%d")

    key = _cache_key(min_lat, max_lat, min_lon, max_lon, date_str)
    if key in _CACHE:
        logger.info("CHLA cache hit for %s", date_str)
        print(f"[CHLA] Cache hit for {date_str} bbox=({min_lat:.1f},{min_lon:.1f})-({max_lat:.1f},{max_lon:.1f})", flush=True)
        return _CACHE[key]

    print(f"[CHLA] Fetching for date={date_str} bbox=({min_lat:.1f},{min_lon:.1f})-({max_lat:.1f},{max_lon:.1f})", flush=True)
    t0 = time.time()
    result = _fetch(copernicusmarine, min_lat, max_lat, min_lon, max_lon, date_str)
    elapsed = time.time() - t0
    if "error" in result:
        print(f"[CHLA] Fetch failed in {elapsed:.1f}s: {result['error']}", flush=True)
    else:
        print(f"[CHLA] Fetch OK in {elapsed:.1f}s — {len(result['points'])} points, date={result['date']}", flush=True)

    if "error" not in result:
        if len(_CACHE) >= _MAX_CACHE:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = result

    return result


# ---------- internals ----------

def _fetch(cm, min_lat, max_lat, min_lon, max_lon, date_str):
    """Try NRT dataset first, fall back to multi-year reprocessed.

    If the requested date exceeds the NRT dataset's latest available date we
    automatically step back to the actual maximum and retry, so that recent
    glider deployments (last_time = today) always resolve.
    """
    last_err = None
    for dataset_id in (_DS_NRT, _DS_MY):
        try:
            return _open_and_extract(cm, dataset_id, min_lat, max_lat, min_lon, max_lon, date_str)
        except Exception as exc:
            msg = str(exc)
            logger.warning("CHLA fetch error (dataset=%s): %s", dataset_id, msg)

            if _is_auth_error(msg):
                return {
                    "error": "Copernicus Marine authentication failed",
                    "hint": (
                        "Run 'copernicusmarine login' in your terminal, "
                        "then restart Glider Playground"
                    ),
                }

            # If our date is beyond the NRT dataset's end, retry with the actual max date
            if dataset_id == _DS_NRT and _is_bounds_error(msg):
                capped = _parse_max_date(msg)
                if capped and capped != date_str:
                    logger.info("Date %s exceeds NRT range — retrying with %s", date_str, capped)
                    try:
                        return _open_and_extract(cm, dataset_id, min_lat, max_lat,
                                                 min_lon, max_lon, capped)
                    except Exception as exc2:
                        logger.warning("CHLA retry error (dataset=%s, date=%s): %s",
                                       dataset_id, capped, exc2)
                        last_err = str(exc2)
                        continue

            last_err = msg

    return {
        "error": f"Could not retrieve CHLA data: {last_err}",
        "hint": "Ensure you are logged in: run 'copernicusmarine login' in your terminal",
    }


def _open_and_extract(cm, dataset_id, min_lat, max_lat, min_lon, max_lon, date_str):
    logger.info("Fetching CHLA from %s for %s", dataset_id, date_str)
    print(f"[CHLA] open_dataset {dataset_id} …", flush=True)
    t0 = time.time()
    ds = cm.open_dataset(
        dataset_id=dataset_id,
        variables=["CHL"],
        minimum_latitude=min_lat,
        maximum_latitude=max_lat,
        minimum_longitude=min_lon,
        maximum_longitude=max_lon,
        start_datetime=f"{date_str}T00:00:00",
        end_datetime=f"{date_str}T23:59:59",
    )
    print(f"[CHLA] open_dataset done in {time.time()-t0:.1f}s", flush=True)
    return _extract(ds, date_str)


def _is_auth_error(msg: str) -> bool:
    low = msg.lower()
    return any(k in low for k in ("401", "403", "unauthorized", "forbidden",
                                  "credentials", "login required", "authentication"))


def _is_bounds_error(msg: str) -> bool:
    low = msg.lower()
    return "exceed" in low and "dataset coordinates" in low


def _parse_max_date(msg: str) -> str | None:
    """Extract the dataset's max available date from a bounds-exceeded error message.

    Copernicus errors look like:
      "...dataset coordinates [2026-04-18 00:00:00+00:00, 2026-05-06 00:00:00+00:00]"
    We want the second (max) date.
    """
    m = re.search(r"dataset coordinates\s*\[.*?,\s*(\d{4}-\d{2}-\d{2})", msg)
    return m.group(1) if m else None


def _extract(ds, date_str: str) -> dict:
    """Pull CHL out of the xarray Dataset and return a flat point list.

    Subsamples the native ~4 km grid by an adaptive stride so the payload stays
    small and the globe's polygon layer is fast to draw — we aim for at most
    _MAX_CELLS_PER_SIDE cells along the longer axis.
    """
    chl_key = next((k for k in ds.data_vars if k.upper() == "CHL"), None)
    if chl_key is None:
        return {"error": "CHL variable not found in dataset", "hint": ""}

    chl_var = ds[chl_key]

    if "time" in chl_var.dims:
        chl_var = chl_var.isel(time=0)

    lat_key = next((k for k in ds.coords if k.lower() in ("latitude", "lat")), None)
    lon_key = next((k for k in ds.coords if k.lower() in ("longitude", "lon")), None)
    if lat_key is None or lon_key is None:
        return {"error": "Could not find lat/lon coordinates in CHLA dataset", "hint": ""}

    lats = ds[lat_key].values.astype(float)
    lons = ds[lon_key].values.astype(float)
    chl = np.array(chl_var.values, dtype=float)

    # Adaptive subsample: keep the largest dimension under the cell cap so the
    # globe stays responsive regardless of how big the glider's region is.
    longest = max(len(lats), len(lons), 1)
    stride = max(1, int(np.ceil(longest / _MAX_CELLS_PER_SIDE)))
    lats = lats[::stride]
    lons = lons[::stride]
    chl = chl[::stride, ::stride]

    # Half-cell size in degrees, from the actual post-subsample lat spacing, so
    # the frontend draws squares that abut cleanly.
    if len(lats) >= 2:
        half_deg = float(abs(lats[1] - lats[0]) / 2.0)
    else:
        half_deg = 0.018 * stride

    lat_grid, lon_grid = np.meshgrid(lats, lons, indexing="ij")
    mask = np.isfinite(chl) & (chl > 0)

    if not np.any(mask):
        return {
            "error": "No valid CHLA data for this region/date (all masked)",
            "hint": "Ocean colour data may be clouded — try toggling again for a different date",
        }

    flat_chl = chl[mask]
    p10 = float(np.percentile(flat_chl, 10))
    p90 = float(np.percentile(flat_chl, 90))

    print(f"[CHLA] Building point list from {int(mask.sum())} valid pixels (stride={stride}) …", flush=True)
    t0pts = time.time()
    points = [
        [round(float(la), 4), round(float(lo), 4), round(float(c), 5)]
        for la, lo, c in zip(lat_grid[mask], lon_grid[mask], flat_chl)
    ]
    print(f"[CHLA] Point list built in {time.time()-t0pts:.2f}s — {len(points)} points (p10={p10:.3f} p90={p90:.3f}, half_deg={half_deg:.4f})", flush=True)

    logger.info("CHLA extracted %d points for %s (p10=%.3f p90=%.3f)",
                len(points), date_str, p10, p90)

    return {"points": points, "date": date_str, "p10": p10, "p90": p90, "half_deg": half_deg}
