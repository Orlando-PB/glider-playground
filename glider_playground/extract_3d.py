import xarray as xr
import pandas as pd
import numpy as np
import json
import os
from pathlib import Path

# --- Tweakable settings ---
QC_SIGMA_THRESHOLD = 15
QC_STUCK_THRESHOLD = 5
MAX_3D_POINTS = 5000

def standalone_qc(vals):
    """
    Quickly strips out fills, zeroes, stuck values, and severe spikes.
    """
    bad_mask = np.zeros(len(vals), dtype=bool)
    
    # 1. Fill values
    bad_mask |= (vals == -999.0) | (vals == -9999.0) | (vals == 999.0) | (vals == 9999.0)
    
    # 2. Zero values (gliders rarely operate exactly at 0,0 lat/lon)
    bad_mask |= (vals == 0.0)
    
    # 3. Stuck values
    diffs = np.diff(vals, append=np.nan)
    stuck_series = pd.Series(diffs == 0)
    consecutive_stuck = stuck_series.rolling(window=QC_STUCK_THRESHOLD, min_periods=1).sum().values
    bad_mask |= (consecutive_stuck >= QC_STUCK_THRESHOLD)
    
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
        
        upper_bound = rolling_median + QC_SIGMA_THRESHOLD * sigma
        lower_bound = rolling_median - QC_SIGMA_THRESHOLD * sigma
        
        sigma_outliers = (clean_vals > upper_bound) | (clean_vals < lower_bound)
        bad_mask[valid_idx[sigma_outliers]] = True
        
    return bad_mask

def extract_for_3d():
    filepath = "/Users/orlpru/Desktop/OG1_Data/input/BIO-Carbon/Nelson_646.nc"
    base_dir = Path(__file__).resolve().parent
    output_path = base_dir / "static" / "test_3d_data.json"
    
    print(f"Reading {filepath}...")
    ds = xr.open_dataset(filepath)
    
    # Extract coordinates and pressure
    df = ds[['LONGITUDE', 'LATITUDE', 'PRES']].to_dataframe().dropna()
    ds.close()
    
    print("Applying QC filters to remove outliers...")
    
    # Apply QC to our three spatial variables
    lat_bad = standalone_qc(df['LATITUDE'].values)
    lon_bad = standalone_qc(df['LONGITUDE'].values)
    pres_bad = standalone_qc(df['PRES'].values)
    
    # Keep the row only if ALL spatial variables are good
    valid_mask = ~lat_bad & ~lon_bad & ~pres_bad
    
    # Basic sanity checks for geography limits
    valid_mask &= (df['LATITUDE'].values >= -90.0) & (df['LATITUDE'].values <= 90.0)
    valid_mask &= (df['LONGITUDE'].values >= -180.0) & (df['LONGITUDE'].values <= 180.0)
    
    df_clean = df[valid_mask]
    points_removed = len(df) - len(df_clean)
    print(f"Removed {points_removed} bad points.")
    
    # Downsample the cleaned glider path
    step = max(1, len(df_clean) // MAX_3D_POINTS)
    df_sub = df_clean.iloc[::step]
    
    # Convert immediately to standard Python floats to prevent JSON serialization errors
    min_lon = float(df_sub['LONGITUDE'].min())
    max_lon = float(df_sub['LONGITUDE'].max())
    min_lat = float(df_sub['LATITUDE'].min())
    max_lat = float(df_sub['LATITUDE'].max())
    
    # Add a 15% buffer around the glider path for the "ocean chunk" boundaries
    lon_pad = float((max_lon - min_lon) * 0.15) or 0.1
    lat_pad = float((max_lat - min_lat) * 0.15) or 0.1
    
    # Fetch Bathymetry using a stable ETOPO dataset via NOAA ERDDAP OPeNDAP
    print("Fetching bathymetry data from NOAA...")
    etopo_url = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/etopo180"
    
    try:
        bathy_ds = xr.open_dataset(etopo_url)
        # Subset the global map to just our padded bounding box
        bathy_subset = bathy_ds.sel(
            latitude=slice(min_lat - lat_pad, max_lat + lat_pad),
            longitude=slice(min_lon - lon_pad, max_lon + lon_pad)
        )
        
        # Subsample bathymetry slightly so the browser doesn't lag rendering the floor
        b_step = max(1, len(bathy_subset.latitude) // 40)
        bathy_subset = bathy_subset.isel(latitude=slice(None, None, b_step), longitude=slice(None, None, b_step))
        
        # .astype(float) ensures we don't pass numpy types to JSON
        b_lon = bathy_subset.longitude.values.astype(float).tolist()
        b_lat = bathy_subset.latitude.values.astype(float).tolist()
        b_z = bathy_subset.altitude.values.astype(float).tolist() 
        bathy_ds.close()
        print("Bathymetry fetched successfully!")
        
    except Exception as e:
        print(f"Warning: Could not fetch bathymetry: {e}")
        print("Falling back to a flat sea floor.")
        # Fallback to a flat floor if NOAA is down
        b_lon = [min_lon - lon_pad, max_lon + lon_pad]
        b_lat = [min_lat - lat_pad, max_lat + lat_pad]
        max_depth = float(df_sub['PRES'].max() + 50)
        b_z = [[-max_depth, -max_depth], [-max_depth, -max_depth]]
        
    data = {
        "lon": df_sub['LONGITUDE'].tolist(),
        "lat": df_sub['LATITUDE'].tolist(),
        # Glider PRES is positive downwards. We multiply by -1 to match GEBCO elevation 
        # so everything exists in the same 3D space (Z=0 is surface, Z=-1000 is deep).
        "elevation": (-df_sub['PRES']).tolist(), 
        "bathy_lon": b_lon,
        "bathy_lat": b_lat,
        "bathy_z": b_z,
        "bounds": {
            "min_lon": min_lon - lon_pad, "max_lon": max_lon + lon_pad,
            "min_lat": min_lat - lat_pad, "max_lat": max_lat + lat_pad
        }
    }
    
    output_path.parent.mkdir(exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f)
        
    print(f"Saved data to {output_path}!")

if __name__ == "__main__":
    extract_for_3d()