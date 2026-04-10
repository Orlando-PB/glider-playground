import os
import time
import functools
import numpy as np
import pandas as pd
import requests
import io
from netCDF4 import Dataset

# Map and 3D Settings
QC_SIGMA_THRESHOLD = 15
QC_STUCK_THRESHOLD = 5
MAX_POINTS = 5000
BATHY_RESOLUTION = 40

def standalone_qc(vals):
    bad_mask = np.zeros(len(vals), dtype=bool)
    
    bad_mask |= (vals == -999.0) | (vals == -9999.0) | (vals == 999.0) | (vals == 9999.0) | (vals == 0.0)
    
    diffs = np.diff(vals, append=np.nan)
    stuck_series = pd.Series(diffs == 0)
    consecutive_stuck = stuck_series.rolling(window=QC_STUCK_THRESHOLD, min_periods=1).sum().values
    bad_mask |= (consecutive_stuck >= QC_STUCK_THRESHOLD)
    
    valid_mask = ~np.isnan(vals) & ~bad_mask
    if np.any(valid_mask):
        valid_idx = np.where(valid_mask)[0]
        clean_vals = vals[valid_mask]
        
        window_size = max(10, len(clean_vals) // 10)
        rolling_median = pd.Series(clean_vals).rolling(window=window_size, center=True, min_periods=1).median().values
        residuals = clean_vals - rolling_median
        
        data_range = np.nanmax(clean_vals) - np.nanmin(clean_vals)
        sigma = max(np.nanstd(residuals), 0.05 * data_range)
        
        upper_bound = rolling_median + QC_SIGMA_THRESHOLD * sigma
        lower_bound = rolling_median - QC_SIGMA_THRESHOLD * sigma
        
        sigma_outliers = (clean_vals > upper_bound) | (clean_vals < lower_bound)
        bad_mask[valid_idx[sigma_outliers]] = True
        
    return bad_mask

@functools.lru_cache(maxsize=32)
def _fetch_bathy_cached(min_lon: float, max_lon: float, min_lat: float, max_lat: float):
    url = f"https://coastwatch.pfeg.noaa.gov/erddap/griddap/etopo180.csv?altitude[({min_lat:.4f}):({max_lat:.4f})][({min_lon:.4f}):({max_lon:.4f})]"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    
    df_b = pd.read_csv(io.StringIO(resp.text), skiprows=[1]).dropna(subset=["altitude"])
    lats = np.sort(df_b["latitude"].unique())
    lons = np.sort(df_b["longitude"].unique())
    
    lat_step = max(1, len(lats) // BATHY_RESOLUTION)
    lon_step = max(1, len(lons) // BATHY_RESOLUTION)
    lats = lats[::lat_step]
    lons = lons[::lon_step]
    
    df_b = df_b[df_b["latitude"].isin(lats) & df_b["longitude"].isin(lons)]
    pivot = df_b.pivot(index="latitude", columns="longitude", values="altitude").reindex(index=lats, columns=lons)
    
    return lons.tolist(), lats.tolist(), pivot.values.tolist()

@functools.lru_cache(maxsize=32)
def get_core_spatial_data(filepath, max_points=MAX_POINTS):
    """Reads NC with netCDF4, applies QC, subsamples, and caches the raw arrays for both 2D and 3D."""
    if not os.path.exists(filepath):
        raise FileNotFoundError("File not found")
        
    nc = Dataset(filepath, 'r')
    if 'LATITUDE' not in nc.variables or 'LONGITUDE' not in nc.variables:
        nc.close()
        raise ValueError("Missing coordinates")
        
    lat = nc.variables['LATITUDE'][:]
    lon = nc.variables['LONGITUDE'][:]
    pres = nc.variables['PRES'][:] if 'PRES' in nc.variables else np.zeros_like(lat)
    temp = nc.variables['TEMP'][:] if 'TEMP' in nc.variables else None
    nc.close()
    
    if len(lat) > max_points:
        step = max(1, len(lat) // max_points)
        lat = lat[::step]
        lon = lon[::step]
        pres = pres[::step]
        if temp is not None:
            temp = temp[::step]
            
    lat = lat.filled(np.nan) if np.ma.isMaskedArray(lat) else lat
    lon = lon.filled(np.nan) if np.ma.isMaskedArray(lon) else lon
    pres = pres.filled(np.nan) if np.ma.isMaskedArray(pres) else pres
    if temp is not None:
        temp = temp.filled(np.nan) if np.ma.isMaskedArray(temp) else temp
        
    valid = ~np.isnan(lat) & ~np.isnan(lon) & ~np.isnan(pres)
    valid &= (lat >= -90.0) & (lat <= 90.0) & (lon >= -180.0) & (lon <= 180.0)
    
    valid &= ~standalone_qc(lat) & ~standalone_qc(lon) & ~standalone_qc(pres)
    
    lat = lat[valid]
    lon = lon[valid]
    pres = pres[valid]
    if temp is not None:
        temp = temp[valid]
        
    if len(lat) == 0:
        raise ValueError("No valid spatial data after QC filters")
        
    return lat, lon, pres, temp

def generate_map_image(filepath):
    t_start = time.time()
    try:
        lat, lon, pres, temp = get_core_spatial_data(filepath)
        t_data = time.time()
        
        path_data = [[round(float(y), 5), round(float(x), 5)] for y, x in zip(lat, lon)]
        t_format = time.time()
        
        return {
            "type": "native_data",
            "path": path_data,
            "timings_seconds": {
                "data_load_qc": round(t_data - t_start, 4),
                "json_formatting": round(t_format - t_data, 4),
                "total": round(t_format - t_start, 4)
            }
        }
    except Exception as e:
        return {"error": str(e)}

def generate_3d_data(filepath):
    try:
        lat, lon, pres, temp = get_core_spatial_data(filepath)
        
        min_lon, max_lon = float(np.min(lon)), float(np.max(lon))
        min_lat, max_lat = float(np.min(lat)), float(np.max(lat))
        
        lon_pad = (max_lon - min_lon) * 0.15 or 0.1
        lat_pad = (max_lat - min_lat) * 0.15 or 0.1
        
        bounds = {
            "min_lon": min_lon - lon_pad, "max_lon": max_lon + lon_pad,
            "min_lat": min_lat - lat_pad, "max_lat": max_lat + lat_pad
        }

        try:
            b_lon, b_lat, b_z = _fetch_bathy_cached(
                round(bounds["min_lon"], 2), round(bounds["max_lon"], 2),
                round(bounds["min_lat"], 2), round(bounds["max_lat"], 2)
            )
        except Exception:
            b_lon = [bounds["min_lon"], bounds["max_lon"]]
            b_lat = [bounds["min_lat"], bounds["max_lat"]]
            floor_depth = float(np.nanmin(pres) * 1.2) if len(pres) > 0 else 1000.0
            b_z = [[floor_depth, floor_depth], [floor_depth, floor_depth]]

        return {
            "lon": lon.tolist(),
            "lat": lat.tolist(),
            "elevation": (-pres).tolist(),
            "temp": [None if np.isnan(t) else float(t) for t in temp] if temp is not None else None,
            "bathy_lon": b_lon,
            "bathy_lat": b_lat,
            "bathy_z": b_z,
            "bounds": bounds
        }
        
    except Exception as e:
        return {"error": f"Internal error: {str(e)}"}