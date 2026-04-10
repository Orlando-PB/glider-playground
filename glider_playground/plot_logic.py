import xarray as xr
import numpy as np
import pandas as pd
from netCDF4 import Dataset, date2num
import datetime
import os

# Locked to 200k points max for optimal WebGL performance
MAX_RENDER_POINTS = 200000

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

def get_dataset_info(filepath):
    if not os.path.exists(filepath):
        return {"error": "File not found"}
    
    nc = Dataset(filepath, 'r')
    
    dims = nc.dimensions
    main_dim_name = next(iter(dims)) if dims else "None"
    main_dim_size = len(dims[main_dim_name]) if dims and main_dim_name in dims else 0
    
    variables = []
    for name, var in nc.variables.items():
        if len(var.dimensions) > 0:
            description = getattr(var, 'long_name', 'No description available')
            variables.append({"name": name, "description": str(description)})
            
    time_stats = {
        "has_time": False, "n_measurements": main_dim_size, "n_valid": main_dim_size,
        "removed_before": 0, "removed_after": 0, "removed_nan": 0,
        "is_monotonic": True, "deploy_time": None
    }
    
    time_name = "TIME" if "TIME" in nc.variables else None
    if not time_name:
        time_vars = [v for v in nc.variables if 'TIME' in v.upper()]
        if time_vars: time_name = time_vars[0]
        
    if time_name:
        time_stats["has_time"] = True
        time_var = nc.variables[time_name]
        time_array = time_var[:]
        
        if np.ma.isMaskedArray(time_array):
            nan_mask = time_array.mask | np.isnan(time_array.data)
            time_array = time_array.data
        else:
            nan_mask = np.isnan(time_array)
            
        time_stats["removed_nan"] = int(nan_mask.sum())
        
        time_units = getattr(time_var, 'units', 'seconds since 1970-01-01 00:00:00')
        time_calendar = getattr(time_var, 'calendar', 'standard')
        
        min_dt = datetime.datetime(1990, 1, 1)
        now_dt = datetime.datetime.now()
        
        try:
            min_time_num = date2num(min_dt, units=time_units, calendar=time_calendar)
            now_time_num = date2num(now_dt, units=time_units, calendar=time_calendar)
        except:
            min_time_num = -np.inf
            now_time_num = np.inf
        
        deploy_dt = None
        if "DEPLOYMENT_TIME" in nc.variables:
            try:
                deploy_val = nc.variables["DEPLOYMENT_TIME"][:]
                if isinstance(deploy_val, np.ndarray) and deploy_val.dtype.kind in 'SU':
                    deploy_dt = pd.to_datetime("".join([c.decode('utf-8') if isinstance(c, bytes) else c for c in deploy_val])).to_pydatetime()
                else:
                    deploy_dt = pd.to_datetime(deploy_val[0]).to_pydatetime()
                    
                time_stats["deploy_time"] = deploy_dt.strftime("%Y-%m-%d %H:%M")
                
                try:
                    deploy_num = date2num(deploy_dt, units=time_units, calendar=time_calendar)
                    min_time_num = max(min_time_num, deploy_num)
                except: pass
            except: pass
        else:
            time_stats["deploy_time"] = "1990-01-01"
            
        with np.errstate(invalid='ignore'):
            before_mask = (time_array < min_time_num) & ~nan_mask
            after_mask = (time_array > now_time_num) & ~nan_mask
            
        time_stats["removed_before"] = int(before_mask.sum())
        time_stats["removed_after"] = int(after_mask.sum())
        
        final_valid_mask = ~(nan_mask | before_mask | after_mask)
        valid_times = time_array[final_valid_mask]
        time_stats["n_valid"] = int(final_valid_mask.sum())

        if len(valid_times) > 1:
            time_stats["is_monotonic"] = bool(np.all(np.diff(valid_times).astype(float) >= 0))

    global_attrs = {attr: str(getattr(nc, attr)) for attr in nc.ncattrs()}
    nc.close()
    
    variables.sort(key=lambda x: x["name"].lower())
    
    return {
        "dimension_name": main_dim_name,
        "dimension_size": main_dim_size,
        "variables": variables,
        "time_stats": time_stats,
        "global_attributes": global_attrs
    }

def get_plot_data_json(filepath, x_var, y_var, c_var="", apply_qc=False, qc_flags="1,2,5,8", highlight_qc=False, filter_time=True):
    if c_var == "None":
        c_var = ""

    glider_data = xr.open_dataset(filepath)
    
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

    valid_mask = ~pd.isnull(x_vals) & ~pd.isnull(y_vals)
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

        if not highlight_qc:
            valid_mask &= qc_pass_mask

    if filter_time and actual_time_var in data_dict:
        t_vals = data_dict[actual_time_var]
        min_time = pd.to_datetime("1990-01-01").to_datetime64()
        now_time = pd.Timestamp.now().to_datetime64()
        with np.errstate(invalid='ignore'):
            time_valid_mask = (t_vals >= min_time) & (t_vals <= now_time) & ~pd.isnull(t_vals)
        valid_mask &= time_valid_mask

    if c_vals is not None:
        if np.issubdtype(c_vals.dtype, np.datetime64):
            c_vals_numeric = np.zeros(len(c_vals), dtype=float)
            c_vals_numeric[:] = np.nan
            valid_dt_mask = ~pd.isnull(c_vals)
            c_vals_numeric[valid_dt_mask] = c_vals[valid_dt_mask].astype('datetime64[s]').astype(float)
            c_vals = c_vals_numeric
        else:
            c_vals = c_vals.astype(float)
        valid_mask &= ~np.isnan(c_vals)

    plot_x = x_vals[valid_mask]
    plot_y = y_vals[valid_mask]
    plot_c = c_vals[valid_mask] if c_vals is not None else None
    plot_qc = qc_pass_mask[valid_mask]

    total_valid_points = len(plot_x)
    if total_valid_points == 0:
        glider_data.close()
        return {"error": "No valid data points remain."}

    # Lock to max points
    if total_valid_points > MAX_RENDER_POINTS:
        step = total_valid_points // MAX_RENDER_POINTS
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
        "qc_applied": apply_qc
    }
def get_plot_data_bounds(filepath, x_var, y_var, c_var="", apply_qc=False, qc_flags="1,2,5,8", 
                          highlight_qc=False, filter_time=True,
                          x_min=None, x_max=None, y_min=None, y_max=None, is_x_dt=False):
    """Like get_plot_data_json but resamples 200k points from within the visible bounds."""
    if c_var == "None":
        c_var = ""

    glider_data = xr.open_dataset(filepath)
    
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

    # --- Apply bounds filter ---
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
    return {"x": x_out, "y": y_out, "c": c_out, "is_x_dt": bool(is_x_dt),
            "c_min": c_min, "c_max": c_max, "qc_applied": apply_qc}