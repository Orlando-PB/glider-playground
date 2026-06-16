"""Derive CTD variables (practical/absolute salinity, conservative temperature,
density) from conductivity using the TEOS-10 / GSW toolbox.

Run once per file during processing, AFTER preload (see cache_logic). Raw glider
conductivity is frequently bad, so the existing CTD cleanup (zero-fill flagging,
CNDC unit scaling to mS/cm, hard range QC, PRES gap interpolation) is applied to
CNDC/TEMP/PRES *before* the GSW calculations. Only variables the file does not
already provide are derived; the results are written to the per-file derived
store (plot_logic) so they appear and behave like native variables everywhere
(variable list, plots, presets, QC search).

This module is intentionally standalone so further one-off derivations (e.g.
backscatter) can be added here as their own functions.
"""

import numpy as np

from . import plot_logic
from . import spatial_logic

try:
    import gsw
    _HAS_GSW = True
except Exception:  # pragma: no cover - import guard
    _HAS_GSW = False

# Flag prefix so the UI makes plain these were NOT in the source file.
_CALC = "⚙️ CALCULATED (not in original file) — "

# CF-ish metadata for each derived variable (adapted from pelagos_py).
DERIVED_METADATA = {
    "PRAC_SALINITY": {"units": "1", "description": _CALC + "Practical salinity, derived from conductivity via TEOS-10/GSW"},
    "ABS_SALINITY": {"units": "g/kg", "description": _CALC + "Absolute salinity, derived via TEOS-10/GSW"},
    "CONS_TEMP": {"units": "degrees_Celsius", "description": _CALC + "Conservative temperature, derived via TEOS-10/GSW"},
    "DENSITY": {"units": "kg/m3", "description": _CALC + "In-situ density, derived via TEOS-10/GSW"},
}


def _interp_over_time(arr, time_vals):
    """Time-linear fill of interior NaN gaps so derived salinity/density are
    continuous. Only fills *between* real samples (no end extrapolation)."""
    import pandas as pd
    a = np.asarray(arr, dtype=float)
    if time_vals is None or not np.any(np.isnan(a)):
        return a
    try:
        t = pd.DatetimeIndex(pd.to_datetime(np.asarray(time_vals), errors="coerce"))
        if len(t) != len(a):
            return a
        valid = ~t.isna()
        if int(valid.sum()) < 2:
            return a
        out = a.copy()
        sub = pd.Series(a[valid], index=t[valid]).interpolate(method="time", limit_area="inside")
        out[valid] = sub.to_numpy()
        return out
    except Exception:
        return a


def _resolve_time_var(names):
    if "TIME" in names:
        return "TIME"
    for n in names:
        if "TIME" in n.upper():
            return n
    return None


def derive_ctd_variables(filepath, log_cb=None):
    """Derive and persist any missing CTD variables for ``filepath``.

    Returns the list of derived variable names (empty if nothing was done).
    Best-effort and never raises for "can't derive" cases — it simply returns
    an empty list when GSW is unavailable, inputs are missing, or the file
    already provides the outputs.
    """
    def log(msg):
        if log_cb:
            try:
                log_cb(msg)
            except Exception:
                pass

    if not _HAS_GSW:
        log("GSW not installed — skipping CTD derivation")
        return []

    # Need conductivity + temperature + pressure (resolves _ADJUSTED variants).
    var_map = plot_logic._resolve_ctd_var_map(filepath)  # canonical -> actual
    if not all(k in var_map for k in ("CNDC", "TEMP", "PRES")):
        return []

    names = list(plot_logic._get_var_names(filepath) or [])
    if not names:
        return []

    # GSW absolute salinity / depth need position.
    lat_name, lon_name = spatial_logic._resolve_latlon_names(names)
    if not lat_name or not lon_name:
        log("No LATITUDE/LONGITUDE — skipping CTD derivation")
        return []
    time_var = _resolve_time_var(names)

    # Only derive variables the file does not already supply (in either the
    # plain or _ADJUSTED form).
    existing = set(names)

    def provided(n):
        return n in existing or (n + "_ADJUSTED") in existing

    wanted = [n for n in ("PRAC_SALINITY", "ABS_SALINITY", "CONS_TEMP", "DENSITY") if not provided(n)]
    if not wanted:
        return []

    # Read just the inputs we need (actual variable names + QC + position + time).
    needed = set(var_map.values())
    for c in ("CNDC", "TEMP", "PRES"):
        needed.add(var_map[c] + "_QC")
    needed.update([lat_name, lon_name])
    if time_var:
        needed.add(time_var)
    data = plot_logic._read_vars_cached(filepath, tuple(sorted(needed)))
    if not data:
        return []

    # CTD cleanup BEFORE deriving: flag zeros, scale CNDC to mS/cm, range-QC,
    # interpolate PRES gaps. Reuses the same path as the interactive "CTD clean
    # + interpolate" overlay so derivation matches what the user sees.
    log("CTD derive: cleaning conductivity / temperature / pressure")
    canon = plot_logic._build_ctd_canonical_dict(data, var_map, time_var)
    cleaned = plot_logic._apply_ctd_processing(
        canon, time_var, plot_logic._get_var_units(filepath),
        interpolate=True, apply_ctd_qc=True,
    )

    try:
        cndc = np.asarray(cleaned["CNDC"], dtype=float)   # mS/cm after cleanup
        temp = np.asarray(cleaned["TEMP"], dtype=float)
        pres = np.asarray(cleaned["PRES"], dtype=float)
        lat = np.asarray(data[lat_name], dtype=float)
        lon = np.asarray(data[lon_name], dtype=float)
    except Exception as e:
        log(f"CTD derive: input read failed ({e})")
        return []

    # Interpolate the cleaned inputs over time so derived salinity/density are
    # continuous (cleanup only fills PRES — CNDC/TEMP gaps would otherwise leave
    # holes in every derived variable).
    tvals = data.get(time_var) if time_var else None
    cndc = _interp_over_time(cndc, tvals)
    temp = _interp_over_time(temp, tvals)
    pres = _interp_over_time(pres, tvals)

    n = len(pres)
    # Position can be per-measurement or a single fix — broadcast a scalar,
    # otherwise require matching lengths.
    def _fit(arr):
        if arr.ndim == 0 or arr.size == 1:
            return np.full(n, float(arr.reshape(-1)[0]) if arr.size else np.nan)
        return arr
    lat, lon = _fit(lat), _fit(lon)
    if not (len(cndc) == len(temp) == n == len(lat) == len(lon)):
        log("CTD derive: input length mismatch — skipping")
        return []

    log(f"CTD derive: computing {', '.join(wanted)} via GSW")
    try:
        sp = gsw.SP_from_C(cndc, temp, pres)            # practical salinity
        sa = gsw.SA_from_SP(sp, pres, lon, lat)         # absolute salinity
        ct = gsw.CT_from_t(sa, temp, pres)              # conservative temperature
        rho = gsw.rho(sa, ct, pres)                     # in-situ density
    except Exception as e:
        log(f"CTD derive: GSW computation failed ({e})")
        return []

    computed = {
        "PRAC_SALINITY": sp,
        "ABS_SALINITY": sa,
        "CONS_TEMP": ct,
        "DENSITY": rho,
    }

    arrays, meta = {}, {}
    for name in wanted:
        arr = np.asarray(computed[name], dtype=float)
        arrays[name] = arr
        meta[name] = {**DERIVED_METADATA[name], "type": "numeric"}
        # A simple QC companion so the QC tooling / search has something: 1 where
        # the value is finite, 9 (missing) where an input was bad/absent.
        qc_name = name + "_QC"
        if not provided(qc_name) and qc_name not in existing:
            arrays[qc_name] = np.where(np.isfinite(arr), 1, 9).astype(np.int8)
            meta[qc_name] = {
                "units": "1", "type": "numeric",
                "description": f"Quality flag for {name} (1=good, 9=missing; derived)",
            }

    plot_logic.save_derived(filepath, arrays, meta)
    log(f"Derived CTD variables: {', '.join(wanted)}")
    return wanted
