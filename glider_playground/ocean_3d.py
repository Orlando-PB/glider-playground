import math
import xarray as xr
import numpy as np
import pandas as pd
import requests
import io
import functools

# --- Tweakable settings ---
QC_SIGMA_THRESHOLD = 15
QC_STUCK_THRESHOLD = 5
MAX_3D_POINTS = 5000
BATHY_RESOLUTION = 40

def standalone_qc(vals):
    """Strips out fills, zeroes, stuck values, and severe spikes."""
    bad_mask = np.zeros(len(vals), dtype=bool)
    
    fills = [-999.0, -9999.0, 999.0, 9999.0, 0.0]
    bad_mask |= np.isin(vals, fills)
    
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
    """
    Fetch bathymetry from ERDDAP using a plain HTTP .csv request.
    Results are cached by bounding box so repeated calls for the same
    region are free. Bounds are rounded to 2 dp to maximise cache hits.
    """
    # Use the ERDDAP griddap CSV endpoint — no HDF5/OPeNDAP driver needed,
    # just a plain HTTP GET that returns a text/csv response.
    url = (
        f"https://coastwatch.pfeg.noaa.gov/erddap/griddap/etopo180.csv"
        f"?altitude[({min_lat:.4f}):({max_lat:.4f})][({min_lon:.4f}):({max_lon:.4f})]"
    )
    
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    
    # ERDDAP CSV: row 0 = headers, row 1 = units — skip both
    df_b = pd.read_csv(io.StringIO(resp.text), skiprows=[1])
    
    # Pivot the flat (latitude, longitude, altitude) table into 2-D arrays
    df_b = df_b.dropna(subset=["altitude"])
    lats = np.sort(df_b["latitude"].unique())
    lons = np.sort(df_b["longitude"].unique())
    
    # Downsample to BATHY_RESOLUTION
    lat_step = max(1, len(lats) // BATHY_RESOLUTION)
    lon_step = max(1, len(lons) // BATHY_RESOLUTION)
    lats = lats[::lat_step]
    lons = lons[::lon_step]
    
    df_b = df_b[df_b["latitude"].isin(lats) & df_b["longitude"].isin(lons)]
    pivot = df_b.pivot(index="latitude", columns="longitude", values="altitude")
    pivot = pivot.reindex(index=lats, columns=lons)
    
    return (
        lons.tolist(),
        lats.tolist(),
        pivot.values.tolist(),
    )


def _round_bounds(value: float, decimals: int = 2) -> float:
    """Round a bound so nearby queries share a cache entry."""
    return round(value, decimals)


def generate_3d_data(filepath: str):
    try:
        with xr.open_dataset(filepath) as ds:
            available = [v for v in ['LONGITUDE', 'LATITUDE', 'PRES', 'TEMP'] if v in ds]
            df = ds[available].to_dataframe().dropna(subset=['LONGITUDE', 'LATITUDE', 'PRES'])

            
            if df.empty:
                return {"error": "Dataset contains no valid spatial data."}

            lat_bad = standalone_qc(df['LATITUDE'].values)
            lon_bad = standalone_qc(df['LONGITUDE'].values)
            pres_bad = standalone_qc(df['PRES'].values)
            
            valid_mask = ~lat_bad & ~lon_bad & ~pres_bad
            valid_mask &= (df['LATITUDE'] >= -90) & (df['LATITUDE'] <= 90)
            valid_mask &= (df['LONGITUDE'] >= -180) & (df['LONGITUDE'] <= 180)
            
            df_clean = df[valid_mask]
            
            if df_clean.empty:
                return {"error": "All data points failed QC filters."}

            step = max(1, len(df_clean) // MAX_3D_POINTS)
            df_sub = df_clean.iloc[::step]
            
            min_lon, max_lon = float(df_sub['LONGITUDE'].min()), float(df_sub['LONGITUDE'].max())
            min_lat, max_lat = float(df_sub['LATITUDE'].min()), float(df_sub['LATITUDE'].max())
            
            lon_pad = (max_lon - min_lon) * 0.15 or 0.1
            lat_pad = (max_lat - min_lat) * 0.15 or 0.1
            
            final_bounds = {
                "min_lon": min_lon - lon_pad, "max_lon": max_lon + lon_pad,
                "min_lat": min_lat - lat_pad, "max_lat": max_lat + lat_pad
            }

            # Round bounds before cache lookup so nearby queries reuse results
            cache_bounds = (
                _round_bounds(final_bounds["min_lon"]),
                _round_bounds(final_bounds["max_lon"]),
                _round_bounds(final_bounds["min_lat"]),
                _round_bounds(final_bounds["max_lat"]),
            )

            try:
                b_lon, b_lat, b_z = _fetch_bathy_cached(*cache_bounds)
                print("Bathymetry fetch successful (from cache or fresh)!")
                
            except Exception as e:
                print(f"Bathy fetch failed (using flat floor): {e}")
                b_lon = [final_bounds["min_lon"], final_bounds["max_lon"]]
                b_lat = [final_bounds["min_lat"], final_bounds["max_lat"]]
                floor_depth = float(np.nanmin(df_sub['PRES'].values) * -1.2)
                b_z = [[floor_depth, floor_depth], [floor_depth, floor_depth]]

            return {
                "lon": df_sub['LONGITUDE'].tolist(),
                "lat": df_sub['LATITUDE'].tolist(),
                "elevation": (-df_sub['PRES']).tolist(),
                # Returns temp if available, otherwise None so the frontend can fall back gracefully
                "temp": df_sub['TEMP'].tolist() if 'TEMP' in df_sub.columns else None,
                "bathy_lon": b_lon,
                "bathy_lat": b_lat,
                "bathy_z": b_z,
                "bounds": final_bounds
            }
            
    except Exception as e:
        print(f"3D processing error: {e}")
        return {"error": f"Internal error: {str(e)}"}