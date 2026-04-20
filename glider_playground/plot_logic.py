import xarray as xr
import numpy as np
import pandas as pd
from netCDF4 import Dataset
import datetime
import os

# Locked to 200k points max for optimal WebGL performance
MAX_RENDER_POINTS = 200000

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

def get_profiles(filepath):
    if not os.path.exists(filepath):
        return {"error": "File not found"}

    try:
        glider_data = xr.open_dataset(filepath)
    except Exception as e:
        return {"error": f"Failed to read dataset: {e}"}

    if "PROFILE_NUMBER" not in glider_data.variables:
        glider_data.close()
        return {"has_profiles": False, "profiles": [], "has_direction": False}

    prof_nums = glider_data.variables["PROFILE_NUMBER"].values.ravel()
    valid_mask = ~pd.isnull(prof_nums)
    prof_nums_valid = prof_nums[valid_mask]

    has_direction = "PROFILE_DIRECTION" in glider_data.variables
    prof_dirs_valid = None
    if has_direction:
        prof_dirs_valid = glider_data.variables["PROFILE_DIRECTION"].values.ravel()[valid_mask]

    if len(prof_nums_valid) == 0:
        glider_data.close()
        return {"has_profiles": False, "profiles": [], "has_direction": has_direction}

    unique_profs = np.unique(prof_nums_valid.astype(float))
    unique_profs = unique_profs[~np.isnan(unique_profs)]

    time_var = None
    if "TIME" in glider_data.variables:
        time_var = "TIME"
    else:
        time_vars = [v for v in glider_data.variables if 'TIME' in v.upper()]
        if time_vars:
            time_var = time_vars[0]

    time_vals_valid = None
    if time_var is not None:
        t_arr = glider_data.variables[time_var].values.ravel()
        if len(t_arr) == len(prof_nums):
            time_vals_valid = t_arr[valid_mask]

    profiles = []
    for p in unique_profs:
        entry = {"number": int(p) if float(p).is_integer() else float(p)}
        p_mask = prof_nums_valid == p
        if has_direction and prof_dirs_valid is not None:
            matches = prof_dirs_valid[p_mask]
            matches = matches[~pd.isnull(matches)]
            if len(matches) > 0:
                entry["direction"] = int(matches[0])
            else:
                entry["direction"] = None
        if time_vals_valid is not None:
            p_times = time_vals_valid[p_mask]
            p_times = p_times[~pd.isnull(p_times)]
            if len(p_times) > 0:
                try:
                    entry["time_min"] = pd.to_datetime(p_times.min()).isoformat()
                    entry["time_max"] = pd.to_datetime(p_times.max()).isoformat()
                except Exception:
                    pass
        profiles.append(entry)

    glider_data.close()
    return {"has_profiles": True, "profiles": profiles, "has_direction": has_direction}


def _apply_profile_mask(data_dict, profile_num):
    if profile_num is None or "PROFILE_NUMBER" not in data_dict:
        return None
    prof_vals = data_dict["PROFILE_NUMBER"].astype(float)
    with np.errstate(invalid='ignore'):
        return (prof_vals == float(profile_num)) & ~np.isnan(prof_vals)


def get_plot_data_json(filepath, x_var, y_var, c_var="", apply_qc=False, qc_flags="1,2,5,8", highlight_qc=False, filter_time=True, profile_num=None):
    if c_var == "None":
        c_var = ""

    if not os.path.exists(filepath):
        return {"error": "File not found"}

    try:
        glider_data = xr.open_dataset(filepath)
    except Exception as e:
        return {"error": f"Failed to read dataset: {e}"}
    
    data_dict = {}
    vars_to_extract = {x_var, y_var}
    if c_var and c_var != 'black':
        vars_to_extract.add(c_var)

    actual_time_var = "TIME"
    if "TIME" not in glider_data.variables:
        time_vars = [v for v in glider_data.variables if 'TIME' in v.upper()]
        if time_vars: actual_time_var = time_vars[0]

    if filter_time and actual_time_var in glider_data.variables:
        vars_to_extract.add(actual_time_var)

    if profile_num is not None and "PROFILE_NUMBER" in glider_data.variables:
        vars_to_extract.add("PROFILE_NUMBER")

    if apply_qc:
        qc_vars = {f"{v}_QC" for v in vars_to_extract}
        vars_to_extract.update(qc_vars)

    for name in vars_to_extract:
        if name in glider_data.variables:
            data_dict[name] = glider_data.variables[name].values.copy().ravel()

    x_vals = data_dict.get(x_var, np.array([]))
    y_vals = data_dict.get(y_var, np.array([]))
    c_vals = data_dict.get(c_var) if c_var and c_var != 'black' else None

    if len(x_vals) == 0:
        glider_data.close()
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
        glider_data.close()
        return {"error": "No valid data points remain.", "stats": stats}

    if stats["valid"] > MAX_RENDER_POINTS:
        step = stats["valid"] // MAX_RENDER_POINTS
        plot_x = plot_x[::step]
        plot_y = plot_y[::step]
        plot_qc = plot_qc[::step]
        if plot_c is not None: plot_c = plot_c[::step]

    is_x_dt = np.issubdtype(plot_x.dtype, np.datetime64)
    
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

    glider_data.close()

    return {
        "x": x_out,
        "y": y_out,
        "c": c_out,
        "is_x_dt": bool(is_x_dt),
        "c_min": c_min,
        "c_max": c_max,
        "qc_applied": apply_qc,
        "qc_pass": plot_qc.tolist() if apply_qc else [],
        "stats": stats
    }

def get_plot_data_bounds(filepath, x_var, y_var, c_var="", apply_qc=False, qc_flags="1,2,5,8",
                          highlight_qc=False, filter_time=True,
                          x_min=None, x_max=None, y_min=None, y_max=None, is_x_dt=False,
                          profile_num=None):
    if c_var == "None":
        c_var = ""

    if not os.path.exists(filepath):
        return {"error": "File not found"}

    try:
        glider_data = xr.open_dataset(filepath)
    except Exception as e:
        return {"error": f"Failed to read dataset: {e}"}
    
    data_dict = {}
    vars_to_extract = {x_var, y_var}
    if c_var and c_var != 'black':
        vars_to_extract.add(c_var)

    actual_time_var = "TIME"
    if "TIME" not in glider_data.variables:
        time_vars = [v for v in glider_data.variables if 'TIME' in v.upper()]
        if time_vars: actual_time_var = time_vars[0]

    if filter_time and actual_time_var in glider_data.variables:
        vars_to_extract.add(actual_time_var)
    if profile_num is not None and "PROFILE_NUMBER" in glider_data.variables:
        vars_to_extract.add("PROFILE_NUMBER")
    if apply_qc:
        vars_to_extract.update({f"{v}_QC" for v in vars_to_extract})

    for name in vars_to_extract:
        if name in glider_data.variables:
            data_dict[name] = glider_data.variables[name].values.copy().ravel()

    x_vals = data_dict.get(x_var, np.array([]))
    y_vals = data_dict.get(y_var, np.array([]))
    c_vals = data_dict.get(c_var) if c_var and c_var != 'black' else None

    if len(x_vals) == 0:
        glider_data.close()
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
        glider_data.close()
        return {"error": "No points in view."}

    if total > MAX_RENDER_POINTS:
        step = total // MAX_RENDER_POINTS
        plot_x = plot_x[::step]
        plot_y = plot_y[::step]
        plot_qc = plot_qc[::step]
        if plot_c is not None:
            plot_c = plot_c[::step]

    is_x_dt = np.issubdtype(plot_x.dtype, np.datetime64)
    x_out = pd.to_datetime(plot_x).strftime('%Y-%m-%d %H:%M:%S').tolist() if is_x_dt else [None if np.isnan(v) else float(v) for v in plot_x]
    y_out = [None if np.isnan(v) else float(v) for v in plot_y]

    c_out, c_min, c_max = [], 0.0, 1.0
    if plot_c is not None:
        c_out = [None if np.isnan(v) else float(v) for v in plot_c]
        valid_c = plot_c[plot_qc] if apply_qc else plot_c
        if len(valid_c) > 0:
            c_min = float(np.nanpercentile(valid_c, 0.1))
            c_max = float(np.nanpercentile(valid_c, 99.9))

    glider_data.close()
    return {
        "x": x_out, "y": y_out, "c": c_out, "is_x_dt": bool(is_x_dt),
        "c_min": c_min, "c_max": c_max, "qc_applied": apply_qc,
        "qc_pass": plot_qc.tolist() if apply_qc else []
    }