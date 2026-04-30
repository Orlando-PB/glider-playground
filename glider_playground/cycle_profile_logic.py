"""Cycle number, SCI_PHASE and direction metadata for glider datasets."""
import os
import functools
import numpy as np
import pandas as pd
import xarray as xr

CYCLE_VAR_NAMES = ["CYCLE_NUMBER", "CYCLE"]


@functools.lru_cache(maxsize=32)
def get_cycles(filepath):
    """Return cycle list with time bounds, plus has_sci_phase / has_direction flags."""
    if not os.path.exists(filepath):
        return {"error": "File not found"}

    try:
        with xr.open_dataset(filepath) as ds:
            var_names = list(ds.variables.keys())
    except Exception as e:
        return {"error": f"Failed to read dataset: {e}"}

    cycle_var = next((v for v in CYCLE_VAR_NAMES if v in var_names), None)
    has_sci_phase = "SCI_PHASE" in var_names
    has_direction = "PROFILE_DIRECTION" in var_names

    base = {"has_sci_phase": has_sci_phase, "has_direction": has_direction}

    if cycle_var is None:
        return {**base, "has_cycles": False, "cycles": [], "cycle_var": None}

    time_var = None
    if "TIME" in var_names:
        time_var = "TIME"
    else:
        time_vars = [v for v in var_names if "TIME" in v.upper()]
        if time_vars:
            time_var = time_vars[0]

    try:
        with xr.open_dataset(filepath) as ds:
            cycle_nums = ds[cycle_var].values.ravel().astype(float)
            time_vals = ds[time_var].values.ravel() if time_var else None
    except Exception as e:
        return {**base, "error": f"Failed to read cycle data: {e}"}

    valid_mask = ~np.isnan(cycle_nums)
    if not valid_mask.any():
        return {**base, "has_cycles": False, "cycles": [], "cycle_var": cycle_var}

    df_dict = {"CYCLE": cycle_nums[valid_mask]}
    if time_vals is not None:
        t_series = pd.to_datetime(time_vals[valid_mask], errors="coerce")
        df_dict["TIME"] = t_series

    df = pd.DataFrame(df_dict)
    grouped = df.groupby("CYCLE", dropna=True)

    t_min = grouped["TIME"].min() if time_var else None
    t_max = grouped["TIME"].max() if time_var else None

    cycles = []
    for c in sorted(grouped.groups.keys()):
        entry = {"number": int(c) if float(c).is_integer() else float(c)}
        if t_min is not None and t_max is not None:
            mn = t_min.get(c)
            mx = t_max.get(c)
            if mn is not pd.NaT and mx is not pd.NaT:
                try:
                    entry["time_min"] = pd.to_datetime(mn).isoformat()
                    entry["time_max"] = pd.to_datetime(mx).isoformat()
                except Exception:
                    pass
        cycles.append(entry)

    return {
        **base,
        "has_cycles": True,
        "cycles": cycles,
        "cycle_var": cycle_var,
    }


def bust_cache(filepath):
    """Invalidate get_cycles cache for the given filepath."""
    try:
        get_cycles.cache_clear()
    except Exception:
        pass
