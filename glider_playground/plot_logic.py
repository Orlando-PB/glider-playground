import xarray as xr
import numpy as np
import pandas as pd
from netCDF4 import Dataset
import datetime
import os
import functools

# Locked to 200k points max for optimal WebGL performance
MAX_RENDER_POINTS = 200000

@functools.lru_cache(maxsize=32)
def _get_var_names(filepath):
    if not os.path.exists(filepath): return []
    try:
        with xr.open_dataset(filepath) as ds:
            return list(ds.variables.keys())
    except Exception:
        return []

@functools.lru_cache(maxsize=16)
def _read_vars_cached(filepath, var_names_tuple):
    if not os.path.exists(filepath): return None
    try:
        with xr.open_dataset(filepath) as ds:
            return {name: ds.variables[name].values.copy().ravel() for name in var_names_tuple if name in ds.variables}
    except Exception:
        return None

@functools.lru_cache(maxsize=32)
def _get_var_units(filepath):
    if not os.path.exists(filepath): return {}
    try:
        with xr.open_dataset(filepath) as ds:
            return {name: str(var.attrs.get('units', '')) for name, var in ds.variables.items()}
    except Exception:
        return {}

@functools.lru_cache(maxsize=32)
def get_variables(filepath):
    if not os.path.exists(filepath):
        return []
    
    try:
        glider_data = xr.open_dataset(filepath)
    except Exception as e:
        print(f"Error opening {filepath}: {e}")
        return []
        
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

@functools.lru_cache(maxsize=32)
def get_dataset_info(filepath):
    if not os.path.exists(filepath):
        return {"error": "File not found"}
    
    try:
        nc = Dataset(filepath, 'r')
    except Exception as e:
        return {"error": f"Unable to read file: {e}"}
    
    dims = nc.dimensions
    main_dim_name = next(iter(dims)) if dims else "None"
    
    variables = []
    for name, var in nc.variables.items():
        if len(var.dimensions) > 0:
            description = getattr(var, 'long_name', 'No description available')
            variables.append({"name": name, "description": str(description)})
            
    global_attrs = {attr: str(getattr(nc, attr)) for attr in nc.ncattrs()}
    nc.close()
    
    variables.sort(key=lambda x: x["name"].lower())
    
    return {
        "dimension_name": main_dim_name,
        "variables": variables,
        "global_attributes": global_attrs
    }

@functools.lru_cache(maxsize=32)
def get_profiles(filepath):
    if not os.path.exists(filepath):
        return {"error": "File not found"}

    var_names = _get_var_names(filepath)
    if not var_names:
        return {"error": "Failed to read dataset"}

    if "PROFILE_NUMBER" not in var_names:
        return {"has_profiles": False, "profiles": [], "has_direction": False}

    has_direction = "PROFILE_DIRECTION" in var_names
    time_var = None
    if "TIME" in var_names:
        time_var = "TIME"
    else:
        time_vars = [v for v in var_names if 'TIME' in v.upper()]
        if time_vars:
            time_var = time_vars[0]

    vars_to_read = {"PROFILE_NUMBER"}
    if has_direction: vars_to_read.add("PROFILE_DIRECTION")
    if time_var: vars_to_read.add(time_var)

    data_dict = _read_vars_cached(filepath, tuple(sorted(vars_to_read)))
    if data_dict is None:
        return {"error": "Failed to extract profile variables"}

    prof_nums = data_dict["PROFILE_NUMBER"]
    valid_mask = ~pd.isnull(prof_nums)

    if not valid_mask.any():
        return {"has_profiles": False, "profiles": [], "has_direction": has_direction}

    df_dict = {"PROFILE_NUMBER": prof_nums[valid_mask]}
    if has_direction:
        df_dict["PROFILE_DIRECTION"] = data_dict["PROFILE_DIRECTION"][valid_mask]
    if time_var:
        df_dict["TIME"] = data_dict[time_var][valid_mask]

    df = pd.DataFrame(df_dict)
    grouped = df.groupby("PROFILE_NUMBER", dropna=True)

    d_first = grouped["PROFILE_DIRECTION"].first() if has_direction else None
    t_min = grouped["TIME"].min() if time_var else None
    t_max = grouped["TIME"].max() if time_var else None

    profiles = []
    for p in grouped.groups.keys():
        entry = {"number": int(p) if float(p).is_integer() else float(p)}
        
        if has_direction and d_first is not None:
            val = d_first.get(p)
            entry["direction"] = None if pd.isnull(val) else int(val)
            
        if time_var and t_min is not None and t_max is not None:
            p_min = t_min.get(p)
            p_max = t_max.get(p)
            if not pd.isnull(p_min) and not pd.isnull(p_max):
                try:
                    entry["time_min"] = pd.to_datetime(p_min).isoformat()
                    entry["time_max"] = pd.to_datetime(p_max).isoformat()
                except Exception:
                    pass
        profiles.append(entry)

    return {"has_profiles": True, "profiles": profiles, "has_direction": has_direction}


def _apply_profile_mask(data_dict, profile_num):
    if profile_num is None or "PROFILE_NUMBER" not in data_dict:
        return None
    prof_vals = data_dict["PROFILE_NUMBER"].astype(float)
    with np.errstate(invalid='ignore'):
        return (prof_vals == float(profile_num)) & ~np.isnan(prof_vals)


def _calculate_mld(plot_x, plot_y, plot_c, is_x_dt):
    if not is_x_dt or len(plot_x) == 0 or plot_c is None:
        return [], []
        
    df = pd.DataFrame({'time': plot_x, 'depth': plot_y, 'temp': plot_c})
    df = df.dropna()
    if len(df) == 0:
        return [], []
        
    # Target ~150 bins across the currently viewed time window for the overlay line
    num_bins = min(150, max(10, len(df) // 100))
    
    # Safely convert datetime to numeric seconds for aggregation
    df['time_num'] = pd.to_numeric(pd.to_datetime(df['time'])) / 10**9
    
    bins = np.linspace(df['time_num'].min(), df['time_num'].max(), num_bins + 1)
    bins = np.unique(bins) # prevent duplicate bin edges
    if len(bins) < 2:
        return [], []
        
    df['bin'] = pd.cut(df['time_num'], bins=bins, include_lowest=True)
    
    mld_x, mld_y = [], []
    
    for name, group in df.groupby('bin', observed=True):
        if len(group) < 5:
            continue
        group = group.sort_values('depth')
        
        # Find reference temperature (median of shallowest 5m)
        min_depth = group['depth'].min()
        shallow = group[group['depth'] <= min_depth + 10.0]
        if len(shallow) == 0:
            shallow = group.head(5)
            
        t_ref = shallow['temp'].median()
        threshold = t_ref - 0.2  # 0.2°C criteria
        
        below_thresh = group[group['temp'] < threshold]
        mld = below_thresh['depth'].min() if len(below_thresh) > 0 else group['depth'].max()
            
        mld_x.append(group['time_num'].mean())
        mld_y.append(mld)
        
    # Smooth the line slightly for visual appeal
    if len(mld_y) > 3:
        mld_y = pd.Series(mld_y).rolling(window=3, center=True, min_periods=1).mean().tolist()
        
    x_out = pd.to_datetime(mld_x, unit='s').strftime('%Y-%m-%d %H:%M:%S').tolist()
    y_out = [float(v) if not pd.isna(v) else None for v in mld_y]
    
    return x_out, y_out


def get_plot_data_json(filepath, x_var, y_var, c_var="", apply_qc=False, qc_flags="1,2,5,8", highlight_qc=False, filter_time=True, profile_num=None, calc_mld=False):
    if isinstance(c_var, str) and "|mld" in c_var:
        calc_mld = True
        c_var = c_var.replace("|mld", "")
        
    if c_var == "None":
        c_var = ""

    if not os.path.exists(filepath):
        return {"error": "File not found"}

    var_names = _get_var_names(filepath)
    if not var_names:
        return {"error": "Failed to read dataset"}

    vars_to_extract = {x_var, y_var}
    if c_var and c_var != 'black':
        vars_to_extract.add(c_var)

    actual_time_var = "TIME"
    if "TIME" not in var_names:
        time_vars = [v for v in var_names if 'TIME' in v.upper()]
        if time_vars: actual_time_var = time_vars[0]

    if filter_time and actual_time_var in var_names:
        vars_to_extract.add(actual_time_var)

    if profile_num is not None and "PROFILE_NUMBER" in var_names:
        vars_to_extract.add("PROFILE_NUMBER")

    if apply_qc:
        qc_vars = {f"{v}_QC" for v in vars_to_extract}
        vars_to_extract.update(qc_vars)

    data_dict = _read_vars_cached(filepath, tuple(sorted(vars_to_extract)))
    if data_dict is None:
        return {"error": "Failed to extract variables from dataset"}

    x_vals = data_dict.get(x_var, np.array([]))
    y_vals = data_dict.get(y_var, np.array([]))
    c_vals = data_dict.get(c_var) if c_var and c_var != 'black' else None

    if len(x_vals) == 0:
        return {"error": "No data found for selected variables."}

    stats = {
        "total": int(len(x_vals)),
        "nan_removed": 0,
        "time_removed": 0,
        "qc_removed": 0,
        "profile_removed": 0,
        "valid": 0
    }

    current_mask = ~pd.isnull(x_vals) & ~pd.isnull(y_vals)

    profile_mask = _apply_profile_mask(data_dict, profile_num)
    if profile_mask is not None:
        old_sum = current_mask.sum()
        current_mask &= profile_mask
        stats["profile_removed"] = int(old_sum - current_mask.sum())
    if c_vals is not None:
        if np.issubdtype(c_vals.dtype, np.datetime64):
            c_vals_numeric = np.zeros(len(c_vals), dtype=float)
            c_vals_numeric[:] = np.nan
            valid_dt_mask = ~pd.isnull(c_vals)
            c_vals_numeric[valid_dt_mask] = c_vals[valid_dt_mask].astype('datetime64[s]').astype(float)
            c_vals = c_vals_numeric
        else:
            c_vals = c_vals.astype(float)
        current_mask &= ~np.isnan(c_vals)
        
    stats["nan_removed"] = int(stats["total"] - current_mask.sum())

    if filter_time and actual_time_var in data_dict:
        t_vals = data_dict[actual_time_var]
        min_time = pd.to_datetime("1990-01-01").to_datetime64()
        now_time = pd.Timestamp.now().to_datetime64()
        with np.errstate(invalid='ignore'):
            time_valid_mask = (t_vals >= min_time) & (t_vals <= now_time) & ~pd.isnull(t_vals)
        
        old_sum = current_mask.sum()
        current_mask &= time_valid_mask
        stats["time_removed"] = int(old_sum - current_mask.sum())

    qc_pass_mask = np.ones(len(x_vals), dtype=bool)
    if apply_qc:
        try:
            allowed_flags = [int(f.strip()) for f in qc_flags.split(',') if f.strip().isdigit()]
        except:
            allowed_flags = [1, 2, 5, 8]
            
        for v in [x_var, y_var, c_var]:
            if v and f"{v}_QC" in data_dict:
                qc_vals = data_dict[f"{v}_QC"]
                qc_pass_mask &= np.isin(qc_vals, allowed_flags)

        if highlight_qc:
            stats["qc_removed"] = int((current_mask & ~qc_pass_mask).sum())
        else:
            old_sum = current_mask.sum()
            current_mask &= qc_pass_mask
            stats["qc_removed"] = int(old_sum - current_mask.sum())

    stats["valid"] = int(current_mask.sum())

    plot_x = x_vals[current_mask]
    plot_y = y_vals[current_mask]
    plot_c = c_vals[current_mask] if c_vals is not None else None
    plot_qc = qc_pass_mask[current_mask]

    if stats["valid"] == 0:
        return {"error": "No valid data points remain.", "stats": stats}

    is_x_dt = np.issubdtype(plot_x.dtype, np.datetime64)
    
    mld_x, mld_y = [], []
    if str(calc_mld).lower() == 'true' and is_x_dt and y_var.upper() in ['PRES', 'GLIDER_DEPTH', 'DEPTH', 'PRES_ENG']:
        mld_x, mld_y = _calculate_mld(plot_x, plot_y, plot_c, is_x_dt)

    if stats["valid"] > MAX_RENDER_POINTS:
        step = stats["valid"] // MAX_RENDER_POINTS
        plot_x = plot_x[::step]
        plot_y = plot_y[::step]
        plot_qc = plot_qc[::step]
        if plot_c is not None: plot_c = plot_c[::step]

    if is_x_dt:
        x_out = pd.to_datetime(plot_x).strftime('%Y-%m-%d %H:%M:%S').tolist()
    else:
        x_out = [None if np.isnan(v) else float(v) for v in plot_x]

    y_out = [None if np.isnan(v) else float(v) for v in plot_y]
    
    c_out = []
    c_min, c_max = 0.0, 1.0
    if plot_c is not None:
        c_out = [None if np.isnan(v) else float(v) for v in plot_c]
        valid_c_for_scale = plot_c[plot_qc] if apply_qc else plot_c
        if len(valid_c_for_scale) > 0:
            c_min = float(np.nanpercentile(valid_c_for_scale, 0.1))
            c_max = float(np.nanpercentile(valid_c_for_scale, 99.9))

    units_map = _get_var_units(filepath)
    return {
        "x": x_out,
        "y": y_out,
        "c": c_out,
        "is_x_dt": bool(is_x_dt),
        "c_min": c_min,
        "c_max": c_max,
        "qc_applied": apply_qc,
        "qc_pass": plot_qc.tolist() if apply_qc else [],
        "stats": stats,
        "mld_x": mld_x,
        "mld_y": mld_y,
        "x_var": x_var,
        "y_var": y_var,
        "c_var": c_var,
        "x_units": units_map.get(x_var, ""),
        "y_units": units_map.get(y_var, ""),
        "c_units": units_map.get(c_var, "") if c_var else ""
    }

def get_plot_data_bounds(filepath, x_var, y_var, c_var="", apply_qc=False, qc_flags="1,2,5,8",
                          highlight_qc=False, filter_time=True,
                          x_min=None, x_max=None, y_min=None, y_max=None, is_x_dt=False,
                          profile_num=None, calc_mld=False):
    if isinstance(c_var, str) and "|mld" in c_var:
        calc_mld = True
        c_var = c_var.replace("|mld", "")

    if c_var == "None":
        c_var = ""

    if not os.path.exists(filepath):
        return {"error": "File not found"}

    var_names = _get_var_names(filepath)
    if not var_names:
        return {"error": "Failed to read dataset"}

    vars_to_extract = {x_var, y_var}
    if c_var and c_var != 'black':
        vars_to_extract.add(c_var)

    actual_time_var = "TIME"
    if "TIME" not in var_names:
        time_vars = [v for v in var_names if 'TIME' in v.upper()]
        if time_vars: actual_time_var = time_vars[0]

    if filter_time and actual_time_var in var_names:
        vars_to_extract.add(actual_time_var)
    if profile_num is not None and "PROFILE_NUMBER" in var_names:
        vars_to_extract.add("PROFILE_NUMBER")
    if apply_qc:
        vars_to_extract.update({f"{v}_QC" for v in vars_to_extract})

    data_dict = _read_vars_cached(filepath, tuple(sorted(vars_to_extract)))
    if data_dict is None:
        return {"error": "Failed to extract variables from dataset"}

    x_vals = data_dict.get(x_var, np.array([]))
    y_vals = data_dict.get(y_var, np.array([]))
    c_vals = data_dict.get(c_var) if c_var and c_var != 'black' else None

    if len(x_vals) == 0:
        return {"error": "No data found."}

    valid_mask = ~pd.isnull(x_vals) & ~pd.isnull(y_vals)
    qc_pass_mask = np.ones(len(x_vals), dtype=bool)

    profile_mask = _apply_profile_mask(data_dict, profile_num)
    if profile_mask is not None:
        valid_mask &= profile_mask

    if apply_qc:
        try:
            allowed_flags = [int(f.strip()) for f in qc_flags.split(',') if f.strip().isdigit()]
        except:
            allowed_flags = [1, 2, 5, 8]
        for v in [x_var, y_var, c_var]:
            if v and f"{v}_QC" in data_dict:
                qc_pass_mask &= np.isin(data_dict[f"{v}_QC"], allowed_flags)
        if not highlight_qc:
            valid_mask &= qc_pass_mask

    if filter_time and actual_time_var in data_dict:
        t_vals = data_dict[actual_time_var]
        min_time = pd.to_datetime("1990-01-01").to_datetime64()
        now_time = pd.Timestamp.now().to_datetime64()
        with np.errstate(invalid='ignore'):
            valid_mask &= (t_vals >= min_time) & (t_vals <= now_time) & ~pd.isnull(t_vals)

    if c_vals is not None:
        if np.issubdtype(c_vals.dtype, np.datetime64):
            c_num = np.full(len(c_vals), np.nan)
            ok = ~pd.isnull(c_vals)
            c_num[ok] = c_vals[ok].astype('datetime64[s]').astype(float)
            c_vals = c_num
        else:
            c_vals = c_vals.astype(float)
        valid_mask &= ~np.isnan(c_vals)

    plot_x = x_vals[valid_mask]
    plot_y = y_vals[valid_mask].astype(float)
    plot_c = c_vals[valid_mask] if c_vals is not None else None
    plot_qc = qc_pass_mask[valid_mask]

    if x_min is not None and x_max is not None:
        is_dt = np.issubdtype(plot_x.dtype, np.datetime64)
        if is_dt:
            x_min_dt = np.datetime64(pd.to_datetime(x_min, unit='ms'))
            x_max_dt = np.datetime64(pd.to_datetime(x_max, unit='ms'))
            bounds_mask = (plot_x >= x_min_dt) & (plot_x <= x_max_dt)
        else:
            bounds_mask = (plot_x.astype(float) >= float(x_min)) & (plot_x.astype(float) <= float(x_max))
        
        if y_min is not None and y_max is not None:
            bounds_mask &= (plot_y >= float(y_min)) & (plot_y <= float(y_max))
        
        plot_x = plot_x[bounds_mask]
        plot_y = plot_y[bounds_mask]
        plot_qc = plot_qc[bounds_mask]
        if plot_c is not None:
            plot_c = plot_c[bounds_mask]

    total = len(plot_x)
    if total == 0:
        return {"error": "No points in view."}

    is_x_dt = np.issubdtype(plot_x.dtype, np.datetime64)

    mld_x, mld_y = [], []
    if str(calc_mld).lower() == 'true' and is_x_dt and y_var.upper() in ['PRES', 'GLIDER_DEPTH', 'DEPTH', 'PRES_ENG']:
        mld_x, mld_y = _calculate_mld(plot_x, plot_y, plot_c, is_x_dt)

    if total > MAX_RENDER_POINTS:
        step = total // MAX_RENDER_POINTS
        plot_x = plot_x[::step]
        plot_y = plot_y[::step]
        plot_qc = plot_qc[::step]
        if plot_c is not None:
            plot_c = plot_c[::step]

    x_out = pd.to_datetime(plot_x).strftime('%Y-%m-%d %H:%M:%S').tolist() if is_x_dt else [None if np.isnan(v) else float(v) for v in plot_x]
    y_out = [None if np.isnan(v) else float(v) for v in plot_y]

    c_out, c_min, c_max = [], 0.0, 1.0
    if plot_c is not None:
        c_out = [None if np.isnan(v) else float(v) for v in plot_c]
        valid_c = plot_c[plot_qc] if apply_qc else plot_c
        if len(valid_c) > 0:
            c_min = float(np.nanpercentile(valid_c, 0.1))
            c_max = float(np.nanpercentile(valid_c, 99.9))

    units_map = _get_var_units(filepath)
    return {
        "x": x_out, "y": y_out, "c": c_out, "is_x_dt": bool(is_x_dt),
        "c_min": c_min, "c_max": c_max, "qc_applied": apply_qc,
        "qc_pass": plot_qc.tolist() if apply_qc else [],
        "mld_x": mld_x, "mld_y": mld_y,
        "x_var": x_var, "y_var": y_var, "c_var": c_var,
        "x_units": units_map.get(x_var, ""),
        "y_units": units_map.get(y_var, ""),
        "c_units": units_map.get(c_var, "") if c_var else ""
    }