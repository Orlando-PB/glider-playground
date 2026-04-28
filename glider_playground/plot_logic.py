import xarray as xr
import numpy as np
import pandas as pd
from netCDF4 import Dataset
import datetime
import os
import functools
import threading

# Locked to 200k points max for optimal WebGL performance
MAX_RENDER_POINTS = 200000

# Filled by cache_logic during processing. When a file is preloaded its
# variable arrays live entirely in RAM, so reads no longer touch the netCDF
# at all. Every cached read path here checks _PRELOADED first.
_PRELOADED: dict[str, dict] = {}
_PRELOADED_LOCK = threading.RLock()


def _bust_caches():
    """Drop every lru_cache that's keyed by filepath. Cheap and safe.

    _ctd_processed_arrays is intentionally excluded: it keys by (filepath,
    interp, clean) so its entries are safe to keep across file loads.
    Busting it would throw away prewarmed CTD results the moment any other
    file is opened, which is why Interpolate felt slow after switching files.
    """
    for fn in (_get_var_names, _read_vars_cached, _get_var_units,
               get_variables, get_dataset_info, get_profiles):
        try:
            fn.cache_clear()
        except AttributeError:
            pass


def set_preloaded(filepath: str, all_vars: dict):
    with _PRELOADED_LOCK:
        _PRELOADED[filepath] = all_vars
    _bust_caches()


def clear_preloaded(filepath: str):
    with _PRELOADED_LOCK:
        _PRELOADED.pop(filepath, None)
    _bust_caches()


def _get_preloaded(filepath: str):
    with _PRELOADED_LOCK:
        return _PRELOADED.get(filepath)

CTD_VARS = ("PRES", "TEMP", "CNDC")
CTD_CNDC_MSCM_UNITS = {"ms/cm", "ms cm-1", "millisiemens/cm", "milli-siemens/cm"}

# For TEMP/CNDC fill: look for real (non-interpolated) samples within this
# time and depth window around the target. If nothing is found the gap is
# left as NaN — glider dive/climb asymmetries can mean there is no honest
# neighbour at the same depth and a naive time interpolation would smear
# bad values across unrelated water layers.

# Set by cache_logic during prewarm so _apply_ctd_processing can report
# fine-grained stage updates. Cleared after prewarm. Not persisted.
_ctd_stage_cb = None


def _report_ctd_stage(msg: str):
    if _ctd_stage_cb is not None:
        try:
            _ctd_stage_cb(msg)
        except Exception:
            pass


@functools.lru_cache(maxsize=12)
def _ctd_processed_arrays(filepath, interpolate: bool, apply_ctd_qc: bool):
    """Pre-compute CTD-processed PRES/TEMP/CNDC (+ their _QC) once per
    (file, interp, qc) combo and cache the result.

    For the (interp=True, qc=True) combo the result is composed from the
    already-cached clean result + a single interpolation pass, rather than
    re-running everything from scratch. If clean changed nothing (already
    clean data) it returns the interp-only result instantly from cache.
    """
    if not (interpolate or apply_ctd_qc):
        return None
    pre = _get_preloaded(filepath)
    if pre is None or not any(v in pre for v in CTD_VARS):
        return None

    time_var = "TIME" if "TIME" in pre else next((v for v in pre if 'TIME' in v.upper()), None)

    needed = set()
    for v in CTD_VARS:
        if v in pre:
            needed.add(v)
            if f"{v}_QC" in pre:
                needed.add(f"{v}_QC")
    if time_var:
        needed.add(time_var)

    if interpolate and apply_ctd_qc:
        # Compose: get the clean overlay (cheap, already cached from prewarm
        # step 1), check if it actually changed anything, then apply only
        # the interpolation step on top — avoids running the slow depth-
        # proximity fill twice.
        clean = _ctd_processed_arrays(filepath, False, True)
        clean_changed = clean is not None and any(
            v in clean and v in pre
            and np.any(np.isnan(clean[v]) != np.isnan(pre[v]))
            for v in CTD_VARS
        )
        if not clean_changed:
            # Data was already clean — interp+clean == interp alone.
            return _ctd_processed_arrays(filepath, True, False)

        # Merge clean results over raw arrays, then interpolate once.
        data_dict = {k: pre[k] for k in needed}
        for k, arr in clean.items():
            if k in data_dict:
                data_dict[k] = arr.copy()
        processed = _apply_ctd_processing(
            data_dict, time_var, _get_var_units(filepath),
            interpolate=True, apply_ctd_qc=False,
        )
    else:
        data_dict = {k: pre[k] for k in needed}
        processed = _apply_ctd_processing(
            data_dict, time_var, _get_var_units(filepath),
            interpolate=interpolate, apply_ctd_qc=apply_ctd_qc,
        )

    overlay = {}
    for v in CTD_VARS:
        if v in processed: overlay[v] = processed[v]
        if f"{v}_QC" in processed: overlay[f"{v}_QC"] = processed[f"{v}_QC"]
    return overlay


def ctd_interp_recommended(filepath) -> bool:
    """True if interpolation fills any PRES gaps for this file.

    Only PRES is interpolated, so this checks whether the overlay actually
    reduces the NaN count in PRES. If PRES is already fully populated the
    button is hidden as it would do nothing visible.
    """
    pre = _get_preloaded(filepath)
    if pre is None or "PRES" not in pre:
        return False
    pres = pre["PRES"]
    if not np.any(np.isnan(pres)):
        return False  # already fully populated — nothing to fill
    overlay = _ctd_processed_arrays(filepath, True, False)
    if overlay is None or "PRES" not in overlay:
        return False
    orig_nan = int(np.isnan(pres).sum())
    new_nan = int(np.isnan(overlay["PRES"]).sum())
    return (orig_nan - new_nan) > 0


def ctd_clean_recommended(filepath) -> bool:
    """True if the Clean step would actually change any values in this file.

    Returns False for pre-processed files where there are no zero fill-values
    and all CNDC readings already fall within [20, 50] mS/cm — in that case
    the button is hidden rather than shown as a no-op.
    """
    pre = _get_preloaded(filepath)
    if pre is None or not any(v in pre for v in CTD_VARS):
        return False
    overlay = _ctd_processed_arrays(filepath, False, True)
    if overlay is None:
        return False
    for v in CTD_VARS:
        if v in overlay and v in pre:
            orig_nan = np.isnan(pre[v])
            new_nan = np.isnan(overlay[v])
            if np.any(new_nan & ~orig_nan):
                return True
    return False


def _apply_ctd_processing(data_dict, time_var, units_map, interpolate=False, apply_ctd_qc=False):
    """Return a new data_dict with CTD interpolation and/or custom QC applied.

    CTD QC: flag exact 0.0 values as 9, auto-scale CNDC from S/m to mS/cm,
    then cross-flag all three CTD vars as 4 where CNDC falls outside [20, 50]
    mS/cm after scaling. Synthesised QC arrays default to 1 (good).

    Interpolate: time-based fill of NaN in the CTD vars. Filled points get
    QC=5 ("value changed"). When combined with CTD QC, bad values are first
    nulled, then interpolation recovers them where a real neighbour exists
    within ±2 h and ±5 m.
    """
    if not (interpolate or apply_ctd_qc):
        return data_dict

    present = [v for v in CTD_VARS if v in data_dict]
    if not present:
        return data_dict

    new_dict = dict(data_dict)
    for v in present:
        new_dict[v] = np.asarray(new_dict[v], dtype=float).copy()
        qc_name = f"{v}_QC"
        if qc_name in new_dict:
            qc_arr = np.asarray(new_dict[qc_name])
            if np.issubdtype(qc_arr.dtype, np.floating):
                qc_arr = np.where(np.isnan(qc_arr), 0, qc_arr)
            new_dict[qc_name] = qc_arr.astype(int).copy()
        else:
            new_dict[qc_name] = np.ones(len(new_dict[v]), dtype=int)

    if apply_ctd_qc:
        _report_ctd_stage("CTD clean: flagging zero fill-values")
        # Zero flagging — treat 0.0 as fill value
        for v in present:
            vals = new_dict[v]
            zero_mask = (vals == 0.0)
            if np.any(zero_mask):
                new_dict[f"{v}_QC"][zero_mask] = 9
                vals[zero_mask] = np.nan

        _report_ctd_stage("CTD clean: scaling CNDC units & range filter")
        # CNDC unit scaling (S/m -> mS/cm) so outlier check sees sensible magnitudes
        if "CNDC" in new_dict:
            cndc_vals = new_dict["CNDC"]
            valid = ~np.isnan(cndc_vals)
            if np.any(valid):
                current_units = str((units_map or {}).get("CNDC", "")).strip().lower()
                already_mscm = current_units in CTD_CNDC_MSCM_UNITS
                if not already_mscm and np.nanmedian(cndc_vals[valid]) < 10.0:
                    cndc_vals[valid] = cndc_vals[valid] * 10.0
                    new_dict["CNDC"] = cndc_vals

        # Hard range filter: CNDC must be in [20, 50] mS/cm; cross-flag all CTD vars
        if "CNDC" in new_dict:
            cndc_vals = new_dict["CNDC"]
            cndc_qc = new_dict["CNDC_QC"]
            valid_for_range = ~np.isnan(cndc_vals) & (cndc_qc != 9)
            if np.any(valid_for_range):
                range_bad = valid_for_range & ((cndc_vals < 20.0) | (cndc_vals > 50.0))
                if np.any(range_bad):
                    for v in present:
                        qc = new_dict[f"{v}_QC"]
                        overwrite = range_bad & ~np.isin(qc, [3, 4, 9])
                        qc[overwrite] = 4
                        new_dict[v][range_bad] = np.nan


    if interpolate and time_var and time_var in new_dict:
        _report_ctd_stage("CTD interp: parsing timestamps")
        t_vals = new_dict[time_var]
        try:
            t_dt = pd.DatetimeIndex(pd.to_datetime(t_vals, errors='coerce'))
        except Exception:
            t_dt = None

        if t_dt is not None and len(t_dt) == len(new_dict[present[0]]):
            min_time = pd.Timestamp("1990-01-01")
            now_time = pd.Timestamp.now()
            nat_mask = np.asarray(pd.isna(t_dt))
            range_ok = np.asarray((t_dt >= min_time) & (t_dt <= now_time))
            valid_time = ~nat_mask & range_ok

            INT_MIN = np.iinfo(np.int64).min
            t_int = t_dt.asi8.copy()
            safe = np.where(valid_time, t_int, INT_MIN)
            run_max = np.maximum.accumulate(safe)
            prev_max = np.empty_like(run_max)
            prev_max[0] = INT_MIN
            prev_max[1:] = run_max[:-1]
            valid_time = valid_time & (t_int >= prev_max)

            if valid_time.any():
                # --- PRES: straight time-linear interpolation ---
                if "PRES" in present:
                    pres = new_dict["PRES"]
                    target = np.isnan(pres) & valid_time
                    _report_ctd_stage(f"CTD interp: filling {int(target.sum())} PRES gaps")
                    if target.any():
                        sub_vals = pres[valid_time]
                        sub_index = t_dt[valid_time]
                        interp_sub = (
                            pd.Series(sub_vals, index=sub_index)
                            .interpolate(method='time', limit_direction='both')
                            .to_numpy()
                        )
                        out = pres.copy()
                        out[valid_time] = interp_sub
                        filled = target & ~np.isnan(out)
                        new_dict["PRES"] = out
                        new_dict["PRES_QC"][filled] = 5


    return new_dict

@functools.lru_cache(maxsize=32)
def _get_var_names(filepath):
    pre = _get_preloaded(filepath)
    if pre is not None:
        return list(pre.keys())
    if not os.path.exists(filepath): return []
    try:
        with xr.open_dataset(filepath) as ds:
            return list(ds.variables.keys())
    except Exception:
        return []

@functools.lru_cache(maxsize=16)
def _read_vars_cached(filepath, var_names_tuple):
    pre = _get_preloaded(filepath)
    if pre is not None:
        return {name: pre[name] for name in var_names_tuple if name in pre}
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
            units = getattr(var, 'units', '')
            variables.append({
                "name": name,
                "description": str(description),
                "units": str(units),
            })
            
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


def get_plot_data_json(filepath, x_var, y_var, c_var="", apply_qc=False, qc_flags="1,2,5,8", highlight_qc=False, filter_time=True, profile_num=None, calc_mld=False, ctd_interpolate=False, ctd_qc=False):
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

    if ctd_interpolate or ctd_qc:
        for v in CTD_VARS:
            if v in var_names:
                vars_to_extract.add(v)
                if f"{v}_QC" in var_names:
                    vars_to_extract.add(f"{v}_QC")
        if actual_time_var in var_names:
            vars_to_extract.add(actual_time_var)

    if apply_qc:
        qc_vars = {f"{v}_QC" for v in vars_to_extract}
        vars_to_extract.update(qc_vars)

    data_dict = _read_vars_cached(filepath, tuple(sorted(vars_to_extract)))
    if data_dict is None:
        return {"error": "Failed to extract variables from dataset"}

    if ctd_interpolate or ctd_qc:
        overlay = _ctd_processed_arrays(filepath, ctd_interpolate, ctd_qc)
        if overlay is not None:
            data_dict = {**data_dict, **overlay}
        else:
            data_dict = _apply_ctd_processing(
                data_dict, actual_time_var, _get_var_units(filepath),
                interpolate=ctd_interpolate, apply_ctd_qc=ctd_qc,
            )

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
                          profile_num=None, calc_mld=False, ctd_interpolate=False, ctd_qc=False):
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
    if ctd_interpolate or ctd_qc:
        for v in CTD_VARS:
            if v in var_names:
                vars_to_extract.add(v)
                if f"{v}_QC" in var_names:
                    vars_to_extract.add(f"{v}_QC")
        if actual_time_var in var_names:
            vars_to_extract.add(actual_time_var)
    if apply_qc:
        vars_to_extract.update({f"{v}_QC" for v in vars_to_extract})

    data_dict = _read_vars_cached(filepath, tuple(sorted(vars_to_extract)))
    if data_dict is None:
        return {"error": "Failed to extract variables from dataset"}

    if ctd_interpolate or ctd_qc:
        overlay = _ctd_processed_arrays(filepath, ctd_interpolate, ctd_qc)
        if overlay is not None:
            data_dict = {**data_dict, **overlay}
        else:
            data_dict = _apply_ctd_processing(
                data_dict, actual_time_var, _get_var_units(filepath),
                interpolate=ctd_interpolate, apply_ctd_qc=ctd_qc,
            )

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