from netCDF4 import Dataset
import numpy as np
import pandas as pd
import os
import time

# Map standalone QC settings
MAP_SIGMA_THRESHOLD = 15
MAP_STUCK_THRESHOLD = 5

def standalone_map_qc(vals):
    """
    Standalone QC just for the map to ensure total independence from the dynamic QC system.
    Quickly strips out fills, zeroes, stuck values, and severe spikes.
    """
    bad_mask = np.zeros(len(vals), dtype=bool)
    
    # 1. Fill values
    bad_mask |= (vals == -999.0) | (vals == -9999.0) | (vals == 999.0) | (vals == 9999.0)
    
    # 2. Zero values
    bad_mask |= (vals == 0.0)
    
    # 3. Stuck values
    diffs = np.diff(vals, append=np.nan)
    stuck_series = pd.Series(diffs == 0)
    consecutive_stuck = stuck_series.rolling(window=MAP_STUCK_THRESHOLD, min_periods=1).sum().values
    bad_mask |= (consecutive_stuck >= MAP_STUCK_THRESHOLD)
    
    # 4. Sigma clip
    valid_mask = ~np.isnan(vals) & ~bad_mask
    if np.any(valid_mask):
        valid_idx = np.where(valid_mask)[0]
        clean_vals = vals[valid_mask]
        
        window_size = max(10, len(clean_vals) // 10)
        rolling_median = pd.Series(clean_vals).rolling(window=window_size, center=True, min_periods=1).median().values
        residuals = clean_vals - rolling_median
        
        data_range = np.nanmax(clean_vals) - np.nanmin(clean_vals)
        sigma = max(np.nanstd(residuals), 0.05 * data_range)
        
        upper_bound = rolling_median + MAP_SIGMA_THRESHOLD * sigma
        lower_bound = rolling_median - MAP_SIGMA_THRESHOLD * sigma
        
        sigma_outliers = (clean_vals > upper_bound) | (clean_vals < lower_bound)
        bad_mask[valid_idx[sigma_outliers]] = True
        
    return bad_mask

def generate_map_image(filepath, max_points=5000):
    t_start = time.time()
    
    if not os.path.exists(filepath):
        return {"error": "File not found"}
        
    try:
        # Phase 1: Load Data
        nc = Dataset(filepath, 'r')
        if 'LATITUDE' not in nc.variables or 'LONGITUDE' not in nc.variables:
            nc.close()
            return {"error": "Missing coordinates"}
            
        lat_full = nc.variables['LATITUDE'][:]
        lon_full = nc.variables['LONGITUDE'][:]
        nc.close()
        
        t_load = time.time()
        
        # Phase 2: Subsample and QC
        total_pts = len(lat_full)
        if total_pts > max_points:
            step = max(1, total_pts // max_points)
            lat_sub = lat_full[::step]
            lon_sub = lon_full[::step]
        else:
            lat_sub = lat_full
            lon_sub = lon_full
            
        if np.ma.isMaskedArray(lat_sub): lat_sub = lat_sub.filled(np.nan)
        if np.ma.isMaskedArray(lon_sub): lon_sub = lon_sub.filled(np.nan)
            
        lat_bad = standalone_map_qc(lat_sub)
        lon_bad = standalone_map_qc(lon_sub)
        
        valid_mask = ~np.isnan(lat_sub) & ~np.isnan(lon_sub)
        valid_mask &= (lat_sub >= -90.0) & (lat_sub <= 90.0)
        valid_mask &= (lon_sub >= -180.0) & (lon_sub <= 180.0)
        valid_mask &= ~lat_bad
        valid_mask &= ~lon_bad
        
        lat_clean = lat_sub[valid_mask]
        lon_clean = lon_sub[valid_mask]
        
        t_qc = time.time()
        
        if len(lat_clean) == 0:
            return {"error": "No valid coordinates after QC"}
            
        # Phase 3: Format for browser
        # Rounding to 5 decimal places gives roughly 1-metre precision, saving bandwidth
        path_data = [[round(float(lat), 5), round(float(lon), 5)] for lat, lon in zip(lat_clean, lon_clean)]
        
        t_format = time.time()
        
        return {
            "type": "native_data",
            "path": path_data,
            "timings_seconds": {
                "load_data": round(t_load - t_start, 4),
                "qc_processing": round(t_qc - t_load, 4),
                "json_formatting": round(t_format - t_qc, 4),
                "total": round(t_format - t_start, 4)
            }
        }
        
    except Exception as e:
        return {"error": str(e)}