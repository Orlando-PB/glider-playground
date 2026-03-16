import xarray as xr
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import io
import base64
import os

MAX_RENDER_POINTS = 100000

def format_dt(dt64):
    return pd.to_datetime(dt64).strftime('%d %B %Y, %H:%M')

def get_variables(filepath):
    if not os.path.exists(filepath):
        return []
    glider_data = xr.open_dataset(filepath)
    variables = []
    for name, var in glider_data.variables.items():
        if len(var.dims) > 0:
            units = var.attrs.get('units', 'No units')
            description = var.attrs.get('long_name', 'No description available')
            dtype_str = str(var.dtype)
            var_type = "datetime" if "datetime" in dtype_str or "M8" in dtype_str else "numeric"
            variables.append({
                "name": name, 
                "units": units, 
                "type": var_type,
                "description": description
            })
    glider_data.close()
    return variables

def generate_sparkline(filepath, x_var, y_var):
    try:
        glider_data = xr.open_dataset(filepath)
        
        actual_x_var = x_var
        if x_var.upper() == 'TIME' and 'TIME' not in glider_data.variables:
            time_vars = [v for v in glider_data.variables if 'TIME' in v.upper()]
            if time_vars: actual_x_var = time_vars[0]

        if y_var not in glider_data.variables or actual_x_var not in glider_data.variables:
            glider_data.close()
            return {"error": "Missing variables"}
            
        x_vals = glider_data[actual_x_var].values.copy().ravel()
        y_vals = glider_data[y_var].values.copy().ravel()
        
        valid_mask = ~pd.isnull(x_vals) & ~pd.isnull(y_vals)
        
        if f"{actual_x_var}_QC" in glider_data.variables:
            qc_vals = glider_data[f"{actual_x_var}_QC"].values.copy().ravel()
            valid_mask &= (qc_vals < 3)

        if f"{y_var}_QC" in glider_data.variables:
            qc_vals = glider_data[f"{y_var}_QC"].values.copy().ravel()
            valid_mask &= (qc_vals < 3)
            
        x_vals = x_vals[valid_mask]
        y_vals = y_vals[valid_mask]
        
        is_x_dt = np.issubdtype(x_vals.dtype, np.datetime64)
        glider_data.close()
            
        if len(x_vals) == 0:
            return {"error": "No valid data"}
            
        sort_idx = np.argsort(x_vals)
        x_vals = x_vals[sort_idx]
        y_vals = y_vals[sort_idx]
            
        if is_x_dt:
            min_x = pd.to_datetime(x_vals[0]).strftime('%Y-%m-%dT%H:%M')
            max_x = pd.to_datetime(x_vals[-1]).strftime('%Y-%m-%dT%H:%M')
        else:
            min_x = float(x_vals[0])
            max_x = float(x_vals[-1])
        
        max_pts = 10000
        if len(x_vals) > max_pts:
            step = len(x_vals) // max_pts
            x_sub = x_vals[::step]
            y_sub = y_vals[::step]
            
            if x_sub[-1] != x_vals[-1]:
                x_sub = np.append(x_sub, x_vals[-1])
                y_sub = np.append(y_sub, y_vals[-1])
        else:
            x_sub = x_vals
            y_sub = y_vals
            
        fig, ax = plt.subplots(figsize=(8, 1), dpi=100)
        ax.plot(x_sub, y_sub, color='#1a73e8', linewidth=1, alpha=0.6)
        ax.axis('off')
        plt.subplots_adjust(top=1, bottom=0, right=1, left=0, hspace=0, wspace=0)
        plt.margins(0,0)
        
        buf = io.BytesIO()
        plt.savefig(buf, format="png", transparent=True, bbox_inches='tight', pad_inches=0)
        buf.seek(0)
        plt.close(fig)
        
        return {
            "image": f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}",
            "min_x": min_x,
            "max_x": max_x
        }
    except Exception as e:
        return {"error": str(e)}

def extract_qc_stats_from_file(data_dict, var_name):
    vals = data_dict.get(var_name, np.array([]))
    stats = {
        "nans": int(pd.isnull(vals).sum()),
        "breakdown": {}, 
        "total_bad": 0,
        "applied": False 
    }
    
    bad_mask = np.zeros(len(vals), dtype=bool)
    qc_var_name = f"{var_name}_QC"
    
    if qc_var_name in data_dict:
        stats["applied"] = True
        qc_vals = data_dict[qc_var_name]
        bad_mask = (qc_vals >= 3) & (qc_vals != 9) & ~pd.isnull(vals)
        stats["total_bad"] = int(np.sum(bad_mask))
            
    return stats, bad_mask

def get_dataset_info(filepath):
    if not os.path.exists(filepath):
        return {"error": "File not found"}
    
    ds = xr.open_dataset(filepath)
    
    dims = dict(ds.sizes)
    main_dim_name = next(iter(dims)) if dims else "None"
    main_dim_size = dims[main_dim_name] if dims else 0
    
    variables = []
    for name, var in ds.variables.items():
        if len(var.dims) > 0:
            description = var.attrs.get('long_name', 'No description available')
            
            variables.append({
                "name": name,
                "description": description
            })
            
    ds.close()
    
    variables.sort(key=lambda x: x["name"].lower())
    
    return {
        "dimension_name": main_dim_name,
        "dimension_size": main_dim_size,
        "variables": variables
    }

def get_nearest_point(filepath, x_var, y_var, c_var, x_val, y_val, is_x_dt, x_min, x_max, y_min, y_max):
    ds = xr.open_dataset(filepath)
    
    actual_x_var = x_var
    if x_var.upper() == 'TIME' and 'TIME' not in ds.variables:
        time_vars = [v for v in ds.variables if 'TIME' in v.upper()]
        if time_vars: actual_x_var = time_vars[0]
        
    x_da = ds[actual_x_var].values.ravel()
    y_da = ds[y_var].values.ravel()
    
    valid = ~pd.isnull(x_da) & ~pd.isnull(y_da)
    if not valid.any(): 
        ds.close()
        return {"error": "No valid data"}
        
    y_floats = y_da[valid].astype(float)
    target_y = float(y_val)
    y_min_f = float(y_min)
    y_max_f = float(y_max)
    y_range = max(y_max_f - y_min_f, 1e-10)
    
    if is_x_dt:
        target_dt = pd.to_datetime(float(x_val), unit='ms')
        x_min_dt = pd.to_datetime(float(x_min), unit='ms')
        x_max_dt = pd.to_datetime(float(x_max), unit='ms')
        
        x_valid_s = pd.to_datetime(x_da[valid]).astype('int64') // 10**9
        target_x = target_dt.timestamp()
        x_min_f = x_min_dt.timestamp()
        x_max_f = x_max_dt.timestamp()
        
        x_floats = x_valid_s.values.astype(float)
        target_x = float(target_x)
    else:
        x_floats = x_da[valid].astype(float)
        target_x = float(x_val)
        x_min_f = float(x_min)
        x_max_f = float(x_max)
        
    x_range = max(x_max_f - x_min_f, 1e-10)
    
    norm_x_diff = (x_floats - target_x) / x_range
    norm_y_diff = (y_floats - target_y) / y_range
    distances = norm_x_diff**2 + norm_y_diff**2
    
    idx = distances.argmin()
    real_idx = np.where(valid)[0][idx]
    
    out = {
        "x": str(x_da[real_idx]),
        "y": float(y_da[real_idx]) if not pd.isnull(y_da[real_idx]) else None,
        "lat": None,
        "lon": None,
        "c_val": None,
        "time_val": None
    }
    
    if c_var and c_var in ds.variables:
        c_data = ds[c_var].values.ravel()
        if not pd.isnull(c_data[real_idx]):
            out["c_val"] = float(c_data[real_idx])
            
    time_name = None
    if 'TIME' in ds.variables:
        time_name = 'TIME'
    else:
        time_vars = [v for v in ds.variables if 'TIME' in v.upper()]
        if time_vars: time_name = time_vars[0]
        
    if time_name:
        t_data = ds[time_name].values.ravel()
        t_val = t_data[real_idx]
        if not pd.isnull(t_val):
            out["time_val"] = pd.to_datetime(t_val).strftime('%Y-%m-%d %H:%M:%S')
    
    if 'LATITUDE' in ds and 'LONGITUDE' in ds:
        lat_arr = ds['LATITUDE'].values.ravel()
        lon_arr = ds['LONGITUDE'].values.ravel()
        
        if time_name:
            time_arr = ds[time_name].values.ravel()
            valid_ll = ~np.isnan(lat_arr) & ~np.isnan(lon_arr) & ~pd.isnull(time_arr)
            
            if valid_ll.any():
                target_time = time_arr[real_idx]
                if not pd.isnull(target_time):
                    t_valid = time_arr[valid_ll].astype(float)
                    sort_idx = np.argsort(t_valid)
                    t_valid = t_valid[sort_idx]
                    lat_valid = lat_arr[valid_ll][sort_idx]
                    lon_valid = lon_arr[valid_ll][sort_idx]
                    
                    target_t_float = target_time.astype(float)
                    out['lat'] = float(np.interp(target_t_float, t_valid, lat_valid))
                    out['lon'] = float(np.interp(target_t_float, t_valid, lon_valid))
        else:
            lat_val = float(lat_arr[real_idx])
            lon_val = float(lon_arr[real_idx])
            out['lat'] = lat_val if not np.isnan(lat_val) else None
            out['lon'] = lon_val if not np.isnan(lon_val) else None
            
    ds.close()
    return out


def generate_plot(filepath, x_var, y_var, c_var="", cmap="viridis", output_path=None, plot_delta=False, delta_axis="x", invert_y=False, trim_start=None, trim_end=None, y_trim_min=None, y_trim_max=None, apply_qc=False, qc_threshold=5):
    # Variables for easy tweaking
    LINE_COLOUR = '#1a73e8'
    TEXT_BOX_BG = '#ffffffcc'
    MAX_MARKER_SIZE = 4
    MIN_MARKER_SIZE = 0.5
    
    glider_data = xr.open_dataset(filepath)
    
    actual_plot_x_var = x_var
    if plot_delta and delta_axis == "time":
        time_vars = [v for v in glider_data.variables if 'TIME' in v.upper()]
        actual_plot_x_var = 'TIME' if 'TIME' in glider_data.variables else (time_vars[0] if time_vars else x_var)
    elif plot_delta and delta_axis == "y":
        actual_plot_x_var = y_var

    data_dict = {}
    vars_to_extract = {actual_plot_x_var, y_var, x_var}
    if c_var and cmap != 'black':
        vars_to_extract.add(c_var)

    if apply_qc:
        qc_vars = {f"{v}_QC" for v in vars_to_extract}
        vars_to_extract.update(qc_vars)

    for name in vars_to_extract:
        if name in glider_data.variables:
            data_dict[name] = glider_data.variables[name].values.copy().ravel()

    x_vals = data_dict.get(actual_plot_x_var, np.array([]))
    y_vals = data_dict.get(y_var, np.array([]))
    raw_comparison_x = data_dict.get(x_var, np.array([]))
    c_vals = data_dict.get(c_var) if c_var and cmap != 'black' else None

    if len(x_vals) == 0:
        glider_data.close()
        raise ValueError("No data found for selected variables.")

    valid_mask = ~pd.isnull(x_vals) & ~pd.isnull(y_vals)

    if apply_qc:
        for v in [actual_plot_x_var, y_var, x_var, c_var]:
            if v and f"{v}_QC" in data_dict:
                qc_vals = data_dict[f"{v}_QC"]
                valid_mask &= (qc_vals <= qc_threshold)

    is_x_dt = np.issubdtype(x_vals.dtype, np.datetime64)
    try:
        if trim_start and str(trim_start).strip() and trim_start != "undefined":
            valid_mask &= (x_vals >= (pd.to_datetime(trim_start).to_datetime64() if is_x_dt else float(trim_start)))
        if trim_end and str(trim_end).strip() and trim_end != "undefined":
            valid_mask &= (x_vals <= (pd.to_datetime(trim_end).to_datetime64() if is_x_dt else float(trim_end)))
        if y_trim_min and str(y_trim_min).strip() and y_trim_min != "undefined":
            valid_mask &= (y_vals >= float(y_trim_min))
        if y_trim_max and str(y_trim_max).strip() and y_trim_max != "undefined":
            valid_mask &= (y_vals <= float(y_trim_max))
    except:
        pass

    plot_y_label = y_var
    if plot_delta:
        if np.issubdtype(y_vals.dtype, np.number):
            y_vals = y_vals - raw_comparison_x
            plot_y_label = f"Δ ({y_var} - {x_var})"

    plot_x = x_vals[valid_mask]
    plot_y = y_vals[valid_mask]
    plot_c = c_vals[valid_mask] if c_vals is not None else None

    total_valid_points = len(plot_x)
    
    if total_valid_points == 0:
        glider_data.close()
        return {"error": "No data points remain in the current view."}

    if total_valid_points > MAX_RENDER_POINTS:
        step = total_valid_points // MAX_RENDER_POINTS
        plot_x, plot_y = plot_x[::step], plot_y[::step]
        if plot_c is not None: plot_c = plot_c[::step]
    
    rendered_points = len(plot_x)
    percentage = (rendered_points / total_valid_points) * 100

    # Calculate dynamic marker size
    # Maps point count [100, 100,000] to size [MAX, MIN]
    dynamic_size = np.interp(rendered_points, [100, MAX_RENDER_POINTS], [MAX_MARKER_SIZE, MIN_MARKER_SIZE])

    fig, ax = plt.subplots(figsize=(18, 8))
    
    if cmap == 'black':
        ax.plot(plot_x, plot_y, marker='.', linestyle='none', markersize=dynamic_size, color='black')
    elif plot_c is not None:
        valid_c = plot_c[~pd.isnull(plot_c)]
        if len(valid_c) > 0:
            c_min = np.percentile(valid_c, 2)
            c_max = np.percentile(valid_c, 98)
        else:
            c_min, c_max = 0, 1
            
        scatter = ax.scatter(plot_x, plot_y, s=dynamic_size**2, c=plot_c, cmap=cmap, vmin=c_min, vmax=c_max)
        plt.colorbar(scatter, label=c_var)
    else:
        ax.plot(plot_x, plot_y, marker='.', linestyle='none', markersize=dynamic_size, color=LINE_COLOUR)

    # Point density info in the bottom right
    info_text = f"{rendered_points:,} / {total_valid_points:,} points ({percentage:.1f}%)"
    ax.text(0.99, 0.02, info_text, transform=ax.transAxes, fontsize=10,
            verticalalignment='bottom', horizontalalignment='right',
            bbox=dict(boxstyle='round,pad=0.5', facecolor=TEXT_BOX_BG, edgecolor='none'))

    if invert_y: ax.invert_yaxis()
    if is_x_dt:
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b %Y\n%H:%M'))
    
    plt.grid(True, linestyle='--', alpha=0.5)
    plt.tight_layout()
    
    fig.canvas.draw()
    bbox = ax.get_position().bounds 
    xlim = list(ax.get_xlim())
    ylim = list(ax.get_ylim())
    if is_x_dt: xlim = [mdates.num2date(xlim[0]).timestamp() * 1000, mdates.num2date(xlim[1]).timestamp() * 1000]

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100)
    plt.close()
    glider_data.close()
    
    return {
        "image": f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}",
        "plot_meta": {"bbox": {"x0": bbox[0], "y0": bbox[1], "w": bbox[2], "h": bbox[3]}, "xlim": xlim, "ylim": ylim, "is_x_dt": bool(is_x_dt)},
        "valid_points": int(len(plot_x))
    }