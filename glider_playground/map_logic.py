import xarray as xr
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import base64
import os

# Map settings
MAX_POINTS = 5000
WATER_COLOUR = '#a1cae6'
LAND_COLOUR = '#81b97a'
LAND_EDGE_COLOUR = '#5a8f53'
PATH_SHADOW_COLOUR = '#333333'
PATH_COLOUR = '#ffeb3b'

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

def generate_map_image(filepath, max_points=MAX_POINTS):
    if not os.path.exists(filepath):
        return {"error": "File not found"}
        
    try:
        glider_data = xr.open_dataset(filepath)
        
        if 'LATITUDE' not in glider_data.variables or 'LONGITUDE' not in glider_data.variables:
            glider_data.close()
            return {"error": "Missing coordinates"}
            
        lat_full = glider_data['LATITUDE'].values.copy()
        lon_full = glider_data['LONGITUDE'].values.copy()
        glider_data.close()
        
        total_pts = len(lat_full)
        if total_pts > max_points:
            step = max(1, total_pts // max_points)
            lat_sub = lat_full[::step]
            lon_sub = lon_full[::step]
        else:
            lat_sub = lat_full
            lon_sub = lon_full
            
        # Apply standalone QC
        lat_bad = standalone_map_qc(lat_sub)
        lon_bad = standalone_map_qc(lon_sub)
        
        valid_mask = ~np.isnan(lat_sub) & ~np.isnan(lon_sub)
        valid_mask &= (lat_sub >= -90.0) & (lat_sub <= 90.0)
        valid_mask &= (lon_sub >= -180.0) & (lon_sub <= 180.0)
        
        valid_mask &= ~lat_bad
        valid_mask &= ~lon_bad
        
        lat_clean = lat_sub[valid_mask]
        lon_clean = lon_sub[valid_mask]
        
        if len(lat_clean) == 0:
            return {"error": "No valid coordinates after QC"}
            
        min_lat, max_lat = np.min(lat_clean), np.max(lat_clean)
        min_lon, max_lon = np.min(lon_clean), np.max(lon_clean)
        
        center_lat = (min_lat + max_lat) / 2.0
        center_lon = (min_lon + max_lon) / 2.0
        
        path_height = max_lat - min_lat
        path_width = max_lon - min_lon
        
        zoom_lat = max(15.0, path_height * 1.5)
        
        aspect_ratio = 1.0 / np.cos(np.radians(center_lat))
        zoom_lon = max(zoom_lat * aspect_ratio, path_width * 1.5)
        
        fig, ax = plt.subplots(figsize=(4, 4), dpi=150)
        
        ax.set_facecolor(WATER_COLOUR)
        
        try:
            import geopandas as gpd
            from geodatasets import get_path
            
            world = gpd.read_file(get_path("naturalearth.land"))
            world.plot(ax=ax, facecolor=LAND_COLOUR, edgecolor=LAND_EDGE_COLOUR, linewidth=0.5, zorder=1)
            
        except Exception as e:
            print(f"Map context skipped: {e}")
            pass 
        
        ax.plot(lon_clean, lat_clean, color=PATH_SHADOW_COLOUR, linewidth=3, zorder=2)
        ax.plot(lon_clean, lat_clean, color=PATH_COLOUR, linewidth=1.5, zorder=3)
        
        ax.set_xlim(center_lon - zoom_lon/2, center_lon + zoom_lon/2)
        ax.set_ylim(center_lat - zoom_lat/2, center_lat + zoom_lat/2)
        
        ax.set_aspect(aspect_ratio, adjustable='box')
        
        ax.axis('off')
        plt.subplots_adjust(top=1, bottom=0, right=1, left=0, hspace=0, wspace=0)
        plt.margins(0,0)
        ax.xaxis.set_major_locator(plt.NullLocator())
        ax.yaxis.set_major_locator(plt.NullLocator())
        
        fig.canvas.draw()
        final_xlim = ax.get_xlim()
        final_ylim = ax.get_ylim()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=150, facecolor=WATER_COLOUR, bbox_inches='tight', pad_inches=0)
        buf.seek(0)
        plt.close(fig)
        
        return {
            "image": f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}",
            "bounds": {
                "min_lon": final_xlim[0],
                "max_lon": final_xlim[1],
                "min_lat": final_ylim[0],
                "max_lat": final_ylim[1]
            }
        }
        
    except Exception as e:
        return {"error": str(e)}