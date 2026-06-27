import hashlib
import json
import struct
import shutil
import xarray as xr
import numpy as np
import pandas as pd
from netCDF4 import Dataset
import datetime
import os
import functools
import threading
import time
from pathlib import Path

# Locked to 200k points max for optimal WebGL performance
MAX_RENDER_POINTS = 200000


# Significant figures kept in the serialized plot arrays. The source sensor
# data is float32 (~7.2 decimal digits), so anything beyond 7 sig figs is pure
# float64 widening noise (e.g. 9.562800407409668 -> 9.5628004). Rounding it away
# is lossless w.r.t. the instrument yet roughly halves the float text gzip ships
# and what the browser has to JSON.parse. Bump this if a genuinely float64
# variable ever needs more — it only trims noise, never bins or drops points.
_PLOT_SIG_FIGS = 7


def _floats_to_list(arr, sig=_PLOT_SIG_FIGS):
    """numpy float array -> JSON-ready Python list, rounded to `sig` significant
    figures, with non-finite (NaN/inf) mapped to None.

    Vectorised end to end (the round, finite test and object cast all run in C),
    so it stays far cheaper than a per-element Python loop. None is required
    because the native (pydantic) JSON serializer emits bare NaN/Infinity tokens
    otherwise, which are invalid JSON and break JSON.parse in the browser.
    """
    arr = np.asarray(arr, dtype=float)
    out = arr.copy()
    nz = np.isfinite(arr) & (arr != 0)
    if nz.any():
        # round to `sig` significant figures: scale so the keepable digits sit
        # left of the decimal, round, scale back.
        mag = np.floor(np.log10(np.abs(arr[nz])))
        factor = 10.0 ** (sig - 1 - mag)
        out[nz] = np.round(arr[nz] * factor) / factor
    obj = out.astype(object)
    obj[~np.isfinite(out)] = None
    return obj.tolist()


# Numpy dtype codes for the binary plot container, all little-endian (every
# deployment target — x86/ARM macOS, Raspberry Pi — is LE, so byte order is fixed).
_BIN_DTYPES = {"f64": "<f8", "f32": "<f4", "u8": "|u1"}


def _pack_plot_binary(meta, arrays):
    """Serialize the bulk plot arrays as a binary container so the browser can map
    them straight into TypedArrays instead of JSON.parse-ing ~500k numbers (the
    cost that dominated the client). Also lets the server skip the
    astype(object)/tolist + JSON text encode.

    Layout: a uint32 LE header length, then a JSON header (all the scalar metadata
    plus an `arrays` descriptor giving each array's dtype + length), then every
    array's raw LE bytes concatenated in the order they appear in `arrays`. NaN is
    preserved in the float arrays (Plotly's scattergl skips NaN points), so no
    null-mapping is needed. f32 matches the float32 source precision; x is f64 so
    datetime epoch-ms stays exact.
    """
    descr = {}
    bufs = []
    for name, arr, code in arrays:
        a = np.ascontiguousarray(arr, dtype=_BIN_DTYPES[code])
        descr[name] = {"dtype": code, "len": int(a.shape[0])}
        bufs.append(a.tobytes())
    header = dict(meta)
    header["arrays"] = descr
    hjson = json.dumps(header).encode("utf-8")
    return b"".join([struct.pack("<I", len(hjson)), hjson, *bufs])

# When LOW_MEMORY_MODE=true all preloaded arrays and CTD overlays are stored
# on disk instead of kept permanently in RAM. Each request loads only what it
# needs, uses it, then the memory is freed. Full prewarming still happens — it
# just writes to the SSD rather than filling RAM.
_LOW_MEMORY = os.getenv("LOW_MEMORY_MODE", "").lower() in ("1", "true", "yes")
_DISK_CACHE_ROOT = Path.home() / ".glider_playground"
_PRELOAD_CACHE_DIR = _DISK_CACHE_ROOT / "preload"
_CTD_CACHE_DIR = _DISK_CACHE_ROOT / "ctd_cache"
if _LOW_MEMORY:
    _PRELOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CTD_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _preload_dir(filepath: str) -> Path:
    h = hashlib.sha256(filepath.encode()).hexdigest()[:16]
    return _PRELOAD_CACHE_DIR / h


def _ctd_cache_path(filepath: str, interpolate: bool, apply_ctd_qc: bool) -> Path:
    h = hashlib.sha256(filepath.encode()).hexdigest()[:16]
    return _CTD_CACHE_DIR / f"{h}_{int(interpolate)}_{int(apply_ctd_qc)}.npz"


# --- Derived variable store (e.g. GSW-derived salinity / density) ------------
# Derived variables are computed once per file during processing (see
# derive_logic) and persisted to disk in BOTH memory modes — in RAM mode the
# preload is dropped on restart and plots fall back to the file, which has no
# derived vars, so we cannot rely on the preload to carry them. They are merged
# into the file's variable list and read path so they behave like native vars.
_DERIVED_CACHE_DIR = _DISK_CACHE_ROOT / "derived"
_DERIVED_META: dict = {}            # filepath -> {name: {"units","description","type"}}
_DERIVED_META_LOCK = threading.RLock()


def _derived_dir(filepath: str) -> Path:
    h = hashlib.sha256(filepath.encode()).hexdigest()[:16]
    return _DERIVED_CACHE_DIR / h


def save_derived(filepath: str, arrays: dict, meta: dict):
    """Persist derived variable arrays + metadata for a file, replacing any
    previous derivation. `meta` is keyed by variable name."""
    clear_derived(filepath)
    d = _derived_dir(filepath)
    d.mkdir(parents=True, exist_ok=True)
    saved_meta = {}
    for name, arr in arrays.items():
        try:
            np.save(str(d / f"{name}.npy"), np.asarray(arr))
            saved_meta[name] = meta.get(name, {})
        except Exception:
            pass
    try:
        (d / "_meta.json").write_text(json.dumps(saved_meta))
    except Exception:
        pass
    with _DERIVED_META_LOCK:
        _DERIVED_META[filepath] = saved_meta
    _bust_caches()


def get_derived_meta(filepath: str) -> dict:
    """Metadata for a file's derived variables ({} if none). Cached in RAM,
    backed by the on-disk sidecar so it survives a restart."""
    with _DERIVED_META_LOCK:
        if filepath in _DERIVED_META:
            return _DERIVED_META[filepath]
    meta = {}
    f = _derived_dir(filepath) / "_meta.json"
    if f.exists():
        try:
            meta = json.loads(f.read_text())
        except Exception:
            meta = {}
    with _DERIVED_META_LOCK:
        _DERIVED_META[filepath] = meta
    return meta


def get_derived_arrays(filepath: str, names) -> dict:
    """Load the requested derived arrays from disk (skips any that are absent)."""
    d = _derived_dir(filepath)
    out = {}
    for name in names:
        f = d / f"{name}.npy"
        if f.exists():
            try:
                out[name] = np.load(str(f), allow_pickle=False)
            except Exception:
                pass
    return out


def clear_derived(filepath: str):
    with _DERIVED_META_LOCK:
        _DERIVED_META.pop(filepath, None)
    shutil.rmtree(_derived_dir(filepath), ignore_errors=True)


def _merge_derived(filepath: str, names, result):
    """Fold any requested derived arrays into a read result, without overriding
    variables the file already supplied."""
    meta = get_derived_meta(filepath)
    if not meta:
        return result
    want = [n for n in names if n in meta and not (result and n in result)]
    if not want:
        return result
    arrs = get_derived_arrays(filepath, want)
    if arrs:
        if result is None:
            result = {}
        result.update(arrs)
    return result


# In RAM mode: maps filepath -> {varname: array, ...}
# In disk mode: maps filepath -> True (sentinel; arrays live on SSD)
_PRELOADED: dict = {}
_PRELOADED_LOCK = threading.RLock()

# The NetCDF/HDF5 C library is NOT thread-safe: concurrent opens/reads (even of
# different files) can segfault the interpreter or return garbled data. FastAPI
# runs sync endpoints on a threadpool and the dashboard fires many read requests
# at once (globe + 3D + every plot panel + variables/profiles), so all NetCDF
# access across the app is serialized through this single process-global lock.
# Preloaded-into-RAM reads don't touch the file and so don't take this lock.
NETCDF_LOCK = threading.RLock()


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


def stream_preload_to_disk(filepath: str, is_removed_fn=None):
    """Stream each variable from the NetCDF directly to disk one at a time.

    Avoids ever accumulating all arrays in RAM simultaneously. After this
    returns, _PRELOADED[filepath] is set to True and arrays live on the SSD.
    """
    import gc
    d = _preload_dir(filepath)
    d.mkdir(parents=True, exist_ok=True)
    names = []
    with NETCDF_LOCK, xr.open_dataset(filepath) as ds:
        for name in ds.variables:
            if is_removed_fn and is_removed_fn():
                return
            try:
                arr = ds.variables[name].values
                arr = arr.copy().ravel() if hasattr(arr, "ravel") else arr
                np.save(str(d / f"{name}.npy"), np.asarray(arr))
                names.append(name)
                del arr
            except Exception:
                pass
    (d / "_names.json").write_text(json.dumps(names))
    with _PRELOADED_LOCK:
        _PRELOADED[filepath] = True
    gc.collect()
    _bust_caches()


def set_preloaded(filepath: str, all_vars: dict):
    if _LOW_MEMORY:
        # Save to disk, then discard from RAM immediately.
        d = _preload_dir(filepath)
        d.mkdir(parents=True, exist_ok=True)
        names = []
        for name in list(all_vars.keys()):
            arr = all_vars.pop(name)
            try:
                np.save(str(d / f"{name}.npy"), np.asarray(arr))
                names.append(name)
            except Exception:
                pass
            del arr
        (d / "_names.json").write_text(json.dumps(names))
        with _PRELOADED_LOCK:
            _PRELOADED[filepath] = True
    else:
        with _PRELOADED_LOCK:
            _PRELOADED[filepath] = all_vars
    _bust_caches()


def clear_preloaded(filepath: str):
    with _PRELOADED_LOCK:
        _PRELOADED.pop(filepath, None)
    if _LOW_MEMORY:
        shutil.rmtree(_preload_dir(filepath), ignore_errors=True)
        h = hashlib.sha256(filepath.encode()).hexdigest()[:16]
        for f in _CTD_CACHE_DIR.glob(f"{h}_*.npz"):
            f.unlink(missing_ok=True)
    clear_derived(filepath)
    _bust_caches()


def _get_preloaded(filepath: str):
    with _PRELOADED_LOCK:
        val = _PRELOADED.get(filepath)
    if val is None:
        return None
    if _LOW_MEMORY:
        d = _preload_dir(filepath)
        names_f = d / "_names.json"
        if not names_f.exists():
            return None
        try:
            names = json.loads(names_f.read_text())
            return {n: np.load(str(d / f"{n}.npy"), allow_pickle=False)
                    for n in names if (d / f"{n}.npy").exists()}
        except Exception:
            return None
    return val

CTD_VARS = ("PRES", "TEMP", "CNDC")
CTD_CNDC_MSCM_UNITS = {"ms/cm", "ms cm-1", "millisiemens/cm", "milli-siemens/cm"}


def _resolve_ctd_var_map(filepath):
    """Map canonical CTD names to the actual file variable, preferring the
    `_ADJUSTED` variant when present so processing operates on the cleaned data."""
    var_names = set(_get_var_names(filepath) or [])
    out = {}
    for v in CTD_VARS:
        adj = f"{v}_ADJUSTED"
        if adj in var_names:
            out[v] = adj
        elif v in var_names:
            out[v] = v
    return out


def _build_ctd_canonical_dict(pre, var_map, time_var):
    """Read preloaded arrays under their actual names and return a dict keyed
    by canonical CTD names (so processing logic can stay name-agnostic)."""
    d = {}
    for canon, actual in var_map.items():
        if actual in pre:
            d[canon] = pre[actual]
            qc = f"{actual}_QC"
            if qc in pre:
                d[f"{canon}_QC"] = pre[qc]
    if time_var and time_var in pre:
        d[time_var] = pre[time_var]
    return d


def _emit_overlay(processed, var_map):
    """Re-key processed canonical results back to actual file variable names."""
    overlay = {}
    for canon, actual in var_map.items():
        if canon in processed:
            overlay[actual] = processed[canon]
        if f"{canon}_QC" in processed:
            overlay[f"{actual}_QC"] = processed[f"{canon}_QC"]
    return overlay


def _overlay_to_canonical(overlay, var_map):
    """Inverse of `_emit_overlay`: convert an actual-keyed overlay back to canonical."""
    canon_dict = {}
    for canon, actual in var_map.items():
        if actual in overlay:
            canon_dict[canon] = overlay[actual]
        qc_actual = f"{actual}_QC"
        if qc_actual in overlay:
            canon_dict[f"{canon}_QC"] = overlay[qc_actual]
    return canon_dict

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
def _ctd_processed_arrays_cached(filepath, interpolate: bool, apply_ctd_qc: bool):
    """RAM-cached CTD overlay — used in normal (non-low-memory) mode.

    For the (interp=True, qc=True) combo the result is composed from the
    already-cached clean result + a single interpolation pass, rather than
    re-running everything from scratch. If clean changed nothing (already
    clean data) it returns the interp-only result instantly from cache.
    """
    if not (interpolate or apply_ctd_qc):
        return None
    pre = _get_preloaded(filepath)
    if pre is None:
        return None
    var_map = _resolve_ctd_var_map(filepath)
    if not var_map:
        return None

    time_var = "TIME" if "TIME" in pre else next((v for v in pre if 'TIME' in v.upper()), None)
    data_dict = _build_ctd_canonical_dict(pre, var_map, time_var)
    if not any(c in data_dict for c in CTD_VARS):
        return None

    if interpolate and apply_ctd_qc:
        clean_actual = _ctd_processed_arrays(filepath, False, True)
        clean_canon = _overlay_to_canonical(clean_actual, var_map) if clean_actual else {}
        clean_changed = bool(clean_canon) and any(
            c in clean_canon and c in data_dict
            and np.any(np.isnan(clean_canon[c]) != np.isnan(data_dict[c]))
            for c in CTD_VARS
        )
        if not clean_changed:
            return _ctd_processed_arrays(filepath, True, False)

        for k, arr in clean_canon.items():
            if k in data_dict:
                data_dict[k] = arr.copy()
        processed = _apply_ctd_processing(
            data_dict, time_var, _get_var_units(filepath),
            interpolate=True, apply_ctd_qc=False,
        )
    else:
        processed = _apply_ctd_processing(
            data_dict, time_var, _get_var_units(filepath),
            interpolate=interpolate, apply_ctd_qc=apply_ctd_qc,
        )

    return _emit_overlay(processed, var_map)


def _ctd_from_disk(filepath, interpolate: bool, apply_ctd_qc: bool):
    """Disk-backed CTD overlay — used in low-memory mode.

    On first call (during prewarm): compute overlay and save as .npz.
    On subsequent calls (plot requests): load .npz, return, GC'd after use.
    No arrays are kept in RAM between requests.
    """
    if not (interpolate or apply_ctd_qc):
        return None
    cache_path = _ctd_cache_path(filepath, interpolate, apply_ctd_qc)
    if cache_path.exists():
        try:
            with np.load(str(cache_path)) as f:
                return dict(f)
        except Exception:
            pass

    pre = _get_preloaded(filepath)
    if pre is None:
        return None
    var_map = _resolve_ctd_var_map(filepath)
    if not var_map:
        return None

    time_var = "TIME" if "TIME" in pre else next((v for v in pre if 'TIME' in v.upper()), None)
    data_dict = _build_ctd_canonical_dict(pre, var_map, time_var)
    if not any(c in data_dict for c in CTD_VARS):
        return None

    if interpolate and apply_ctd_qc:
        clean_actual = _ctd_from_disk(filepath, False, True)
        clean_canon = _overlay_to_canonical(clean_actual, var_map) if clean_actual else {}
        clean_changed = bool(clean_canon) and any(
            c in clean_canon and c in data_dict
            and np.any(np.isnan(clean_canon[c]) != np.isnan(data_dict[c]))
            for c in CTD_VARS
        )
        if not clean_changed:
            return _ctd_from_disk(filepath, True, False)

        for k, arr in clean_canon.items():
            if k in data_dict:
                data_dict[k] = arr.copy()
        processed = _apply_ctd_processing(
            data_dict, time_var, _get_var_units(filepath),
            interpolate=True, apply_ctd_qc=False,
        )
    else:
        processed = _apply_ctd_processing(
            data_dict, time_var, _get_var_units(filepath),
            interpolate=interpolate, apply_ctd_qc=apply_ctd_qc,
        )

    overlay = _emit_overlay(processed, var_map)

    if overlay:
        try:
            np.savez(str(cache_path), **overlay)
        except Exception:
            pass

    return overlay or None


def _ctd_processed_arrays(filepath, interpolate: bool, apply_ctd_qc: bool):
    """Public entry point — dispatches to disk or RAM cache depending on mode."""
    if _LOW_MEMORY:
        return _ctd_from_disk(filepath, interpolate, apply_ctd_qc)
    return _ctd_processed_arrays_cached(filepath, interpolate, apply_ctd_qc)


def ctd_interp_recommended(filepath) -> bool:
    """True if interpolation fills any PRES gaps for this file.

    Only PRES is interpolated, so this checks whether the overlay actually
    reduces the NaN count in PRES. If PRES is already fully populated the
    button is hidden as it would do nothing visible.
    """
    pre = _get_preloaded(filepath)
    if pre is None:
        return False
    var_map = _resolve_ctd_var_map(filepath)
    pres_actual = var_map.get("PRES")
    if not pres_actual or pres_actual not in pre:
        return False
    pres = pre[pres_actual]
    if not np.any(np.isnan(pres)):
        return False  # already fully populated — nothing to fill
    overlay = _ctd_processed_arrays(filepath, True, False)
    if overlay is None or pres_actual not in overlay:
        return False
    orig_nan = int(np.isnan(pres).sum())
    new_nan = int(np.isnan(overlay[pres_actual]).sum())
    return (orig_nan - new_nan) > 0


def ctd_clean_recommended(filepath) -> bool:
    """True if the Clean step would actually change any values in this file.

    Returns False for pre-processed files where there are no zero fill-values
    and all CNDC readings already fall within [20, 50] mS/cm — in that case
    the button is hidden rather than shown as a no-op.
    """
    pre = _get_preloaded(filepath)
    if pre is None:
        return False
    var_map = _resolve_ctd_var_map(filepath)
    if not var_map:
        return False
    overlay = _ctd_processed_arrays(filepath, False, True)
    if overlay is None:
        return False
    for canon, actual in var_map.items():
        if actual in overlay and actual in pre:
            orig_nan = np.isnan(pre[actual])
            new_nan = np.isnan(overlay[actual])
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
    names = None
    if _LOW_MEMORY:
        with _PRELOADED_LOCK:
            is_preloaded = filepath in _PRELOADED
        if is_preloaded:
            names_f = _preload_dir(filepath) / "_names.json"
            if names_f.exists():
                try:
                    names = json.loads(names_f.read_text())
                except Exception:
                    names = None
    else:
        pre = _get_preloaded(filepath)
        if pre is not None:
            names = list(pre.keys())
    if names is None:
        if not os.path.exists(filepath):
            names = []
        else:
            try:
                with NETCDF_LOCK, xr.open_dataset(filepath) as ds:
                    names = list(ds.variables.keys())
            except Exception:
                names = []
    der = list(get_derived_meta(filepath).keys())
    if der:
        names = list(dict.fromkeys(list(names) + der))
    return names

@functools.lru_cache(maxsize=4 if _LOW_MEMORY else 16)
def _read_vars_cached(filepath, var_names_tuple):
    result = None
    if _LOW_MEMORY:
        with _PRELOADED_LOCK:
            is_preloaded = filepath in _PRELOADED
        if is_preloaded:
            d = _preload_dir(filepath)
            result = {}
            for name in var_names_tuple:
                f = d / f"{name}.npy"
                if f.exists():
                    try:
                        result[name] = np.load(str(f), allow_pickle=False)
                    except Exception:
                        pass
    else:
        pre = _get_preloaded(filepath)
        if pre is not None:
            result = {name: pre[name] for name in var_names_tuple if name in pre}
    if result is None and os.path.exists(filepath):
        try:
            with NETCDF_LOCK, xr.open_dataset(filepath) as ds:
                result = {name: ds.variables[name].values.copy().ravel() for name in var_names_tuple if name in ds.variables}
        except Exception:
            result = None
    # Fold in any requested variables that were derived post-preload.
    result = _merge_derived(filepath, var_names_tuple, result)
    return result or None

@functools.lru_cache(maxsize=32)
def _get_var_units(filepath):
    units = {}
    if os.path.exists(filepath):
        try:
            with NETCDF_LOCK, xr.open_dataset(filepath) as ds:
                units = {name: str(var.attrs.get('units', '')) for name, var in ds.variables.items()}
        except Exception:
            units = {}
    for name, m in get_derived_meta(filepath).items():
        units.setdefault(name, m.get("units", ""))
    return units

@functools.lru_cache(maxsize=32)
def get_variables(filepath):
    if not os.path.exists(filepath):
        return []
    
    variables = []
    try:
        with NETCDF_LOCK, xr.open_dataset(filepath) as glider_data:
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
    except Exception as e:
        print(f"Error opening {filepath}: {e}")
        return []
    existing = {v["name"] for v in variables}
    for name, m in get_derived_meta(filepath).items():
        if name in existing:
            continue
        variables.append({
            "name": name,
            "units": m.get("units") or "No units",
            "type": "numeric",
            "description": m.get("description") or "No description available",
            "derived": True,
        })
    return variables

@functools.lru_cache(maxsize=32)
def get_dataset_info(filepath):
    if not os.path.exists(filepath):
        return {"error": "File not found"}
    
    try:
        with NETCDF_LOCK, Dataset(filepath, 'r') as nc:
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
    except Exception as e:
        return {"error": f"Unable to read file: {e}"}

    existing = {v["name"] for v in variables}
    for name, m in get_derived_meta(filepath).items():
        if name in existing:
            continue
        variables.append({
            "name": name,
            "description": m.get("description") or "No description available",
            "units": m.get("units") or "",
            "derived": True,
        })

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


def _apply_cycle_mask(data_dict, cycle_num, cycle_var):
    if cycle_num is None or not cycle_var or cycle_var not in data_dict:
        return None
    vals = data_dict[cycle_var].astype(float)
    with np.errstate(invalid='ignore'):
        return (vals == float(cycle_num)) & ~np.isnan(vals)


def _apply_phase_mask(data_dict, sci_phases):
    if not sci_phases or "SCI_PHASE" not in data_dict:
        return None
    allowed = [float(p) for p in sci_phases]
    phase_vals = data_dict["SCI_PHASE"].astype(float)
    with np.errstate(invalid='ignore'):
        return np.isin(phase_vals, allowed) & ~np.isnan(phase_vals)


def _apply_direction_mask(data_dict, directions):
    if not directions or "PROFILE_DIRECTION" not in data_dict:
        return None
    allowed = [float(d) for d in directions]
    dir_vals = data_dict["PROFILE_DIRECTION"].astype(float)
    with np.errstate(invalid='ignore'):
        return np.isin(dir_vals, allowed) & ~np.isnan(dir_vals)


def get_plot_data_json(filepath, x_var, y_var, c_var="", apply_qc=False, qc_flags="1,2,5,8", highlight_qc=False, filter_time=True, profile_num=None, cycle_num=None, cycle_var=None, sci_phases=None, direction_filter=None, ctd_interpolate=False, ctd_qc=False, highlight_profile=False, max_points=None, zoom_x_var=None, zoom_x_min=None, zoom_x_max=None, zoom_y_min=None, zoom_y_max=None, timings=None, binary=False):
    # `timings` (optional dict) is filled in place with per-step ms so the endpoint
    # can surface a Server-Timing breakdown. perf_counter / dict writes are ~free.
    _tprev = time.perf_counter()
    def _mark(name):
        nonlocal _tprev
        if timings is not None:
            now = time.perf_counter()
            timings[name] = timings.get(name, 0.0) + (now - _tprev) * 1000.0
            _tprev = now

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

    if cycle_num is not None and cycle_var and cycle_var in var_names:
        vars_to_extract.add(cycle_var)

    if sci_phases and "SCI_PHASE" in var_names:
        vars_to_extract.add("SCI_PHASE")

    if direction_filter and "PROFILE_DIRECTION" in var_names:
        vars_to_extract.add("PROFILE_DIRECTION")

    # When the marginal greys points outside a zoom window, the window is in the
    # main plot's x_var space (which differs from this fetch's x_var = colour
    # variable), so the original x_var column has to be loaded alongside.
    if highlight_profile and zoom_x_var and zoom_x_var in var_names:
        vars_to_extract.add(zoom_x_var)

    ctd_var_map = _resolve_ctd_var_map(filepath) if (ctd_interpolate or ctd_qc) else {}
    if ctd_interpolate or ctd_qc:
        for actual in ctd_var_map.values():
            vars_to_extract.add(actual)
            if f"{actual}_QC" in var_names:
                vars_to_extract.add(f"{actual}_QC")
        if actual_time_var in var_names:
            vars_to_extract.add(actual_time_var)

    if apply_qc:
        qc_vars = {f"{v}_QC" for v in vars_to_extract}
        vars_to_extract.update(qc_vars)

    data_dict = _read_vars_cached(filepath, tuple(sorted(vars_to_extract)))
    if data_dict is None:
        return {"error": "Failed to extract variables from dataset"}
    _mark("read")

    if ctd_interpolate or ctd_qc:
        overlay = _ctd_processed_arrays(filepath, ctd_interpolate, ctd_qc)
        if overlay is not None:
            data_dict = {**data_dict, **overlay}
        else:
            canon = _build_ctd_canonical_dict(data_dict, ctd_var_map, actual_time_var)
            processed = _apply_ctd_processing(
                canon, actual_time_var, _get_var_units(filepath),
                interpolate=ctd_interpolate, apply_ctd_qc=ctd_qc,
            )
            data_dict = {**data_dict, **_emit_overlay(processed, ctd_var_map)}
        _mark("ctd")

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
    cycle_mask = _apply_cycle_mask(data_dict, cycle_num, cycle_var)
    phase_mask = _apply_phase_mask(data_dict, sci_phases)
    dir_mask = _apply_direction_mask(data_dict, direction_filter)

    # Combined selection (profile ∧ cycle ∧ phase ∧ direction) — None if no filter.
    selection_mask = None
    for m in (profile_mask, cycle_mask, phase_mask, dir_mask):
        if m is not None:
            selection_mask = m if selection_mask is None else (selection_mask & m)

    # Zoom highlight: with the marginal active and the main plot zoomed, treat
    # "inside the current view" exactly like a selection — colour the in-view
    # points and grey the rest. The window is in the main plot's x_var / depth
    # space, so test the main x column and depth (y_var) here. Folds into the
    # selection mask so a profile/cycle filter and the zoom narrow it together.
    if (highlight_profile and zoom_x_var
            and zoom_x_min is not None and zoom_x_max is not None
            and zoom_y_min is not None and zoom_y_max is not None):
        zx = data_dict.get(zoom_x_var)
        if zx is not None and len(zx) == len(x_vals):
            if np.issubdtype(zx.dtype, np.datetime64):
                zx_lo = np.datetime64(pd.to_datetime(zoom_x_min, unit='ms'))
                zx_hi = np.datetime64(pd.to_datetime(zoom_x_max, unit='ms'))
                with np.errstate(invalid='ignore'):
                    zoom_mask = (zx >= zx_lo) & (zx <= zx_hi)
            else:
                zxf = np.asarray(zx, dtype=float)
                with np.errstate(invalid='ignore'):
                    zoom_mask = (zxf >= float(zoom_x_min)) & (zxf <= float(zoom_x_max))
            if not np.issubdtype(y_vals.dtype, np.datetime64):
                yf = np.asarray(y_vals, dtype=float)
                y_lo, y_hi = sorted((float(zoom_y_min), float(zoom_y_max)))
                with np.errstate(invalid='ignore'):
                    zoom_mask &= (yf >= y_lo) & (yf <= y_hi)
            selection_mask = zoom_mask if selection_mask is None else (selection_mask & zoom_mask)

    if highlight_profile:
        # Keep every point; the selection is returned as a per-point flag so the
        # frontend can grey the context and colour only the selection on top.
        if profile_mask is not None:
            stats["profile_removed"] = 0
    else:
        if profile_mask is not None:
            old_sum = current_mask.sum()
            current_mask &= profile_mask
            stats["profile_removed"] = int(old_sum - current_mask.sum())
        if cycle_mask is not None:
            current_mask &= cycle_mask
        if phase_mask is not None:
            current_mask &= phase_mask
        if dir_mask is not None:
            current_mask &= dir_mask

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
    _mark("filter")

    plot_x = x_vals[current_mask]
    plot_y = y_vals[current_mask]
    plot_c = c_vals[current_mask] if c_vals is not None else None
    plot_qc = qc_pass_mask[current_mask]
    # Per-point "is in the active selection" flag (highlight mode only).
    plot_sel = selection_mask[current_mask] if (highlight_profile and selection_mask is not None) else None

    if stats["valid"] == 0:
        return {"error": "No valid data points remain.", "stats": stats}

    is_x_dt = np.issubdtype(plot_x.dtype, np.datetime64)

    render_cap = max_points if (max_points and max_points > 0) else MAX_RENDER_POINTS
    if plot_sel is not None and plot_sel.any() and not plot_sel.all():
        # Highlight mode with an active selection: keep the selected points (e.g. the
        # chosen profile/cycle) at full detail and only thin the grey context, so the
        # highlighted cast is rich rather than decimated to a few survivors.
        sel_idx = np.nonzero(plot_sel)[0]
        ctx_idx = np.nonzero(~plot_sel)[0]
        SEL_CAP = 20000
        if len(sel_idx) > SEL_CAP:
            sel_idx = sel_idx[:: int(np.ceil(len(sel_idx) / SEL_CAP))]
        if len(ctx_idx) > render_cap:
            ctx_idx = ctx_idx[:: int(np.ceil(len(ctx_idx) / render_cap))]
        keep = np.sort(np.concatenate([sel_idx, ctx_idx]))
        plot_x = plot_x[keep]; plot_y = plot_y[keep]; plot_qc = plot_qc[keep]; plot_sel = plot_sel[keep]
        if plot_c is not None: plot_c = plot_c[keep]
    elif stats["valid"] > render_cap:
        # ceil, not floor: valid // cap floors to step=1 whenever valid < 2*cap
        # (e.g. 245386 // 200000 == 1), so the cap leaked up to ~2x its limit and
        # decimated nothing. ceil guarantees the result is <= render_cap.
        step = int(np.ceil(stats["valid"] / render_cap))
        plot_x = plot_x[::step]
        plot_y = plot_y[::step]
        plot_qc = plot_qc[::step]
        if plot_c is not None: plot_c = plot_c[::step]
        if plot_sel is not None: plot_sel = plot_sel[::step]
    _mark("downsample")

    # Datetime x is emitted as epoch-ms integers (UTC) rather than formatted
    # strings: the vectorised astype is ~10x faster than per-element strftime (~23ms
    # -> ~2ms on 160k points) and the payload is ~35% smaller. Plotly's date axis
    # (xaxis.type='date') consumes ms-since-epoch directly, and ms even preserves
    # sub-second precision the old '%Y-%m-%d %H:%M:%S' format dropped. Nulls were
    # already removed by current_mask, so a plain tolist() is safe.
    # Colour range — needed by both serializers, independent of array encoding.
    c_min, c_max = 0.0, 1.0
    if plot_c is not None:
        valid_c_for_scale = plot_c[plot_qc] if apply_qc else plot_c
        if len(valid_c_for_scale) > 0:
            c_min = float(np.nanpercentile(valid_c_for_scale, 0.1))
            c_max = float(np.nanpercentile(valid_c_for_scale, 99.9))

    units_map = _get_var_units(filepath)
    meta = {
        "is_x_dt": bool(is_x_dt),
        "c_min": c_min,
        "c_max": c_max,
        "qc_applied": apply_qc,
        "profile_highlight": bool(highlight_profile and selection_mask is not None),
        "stats": stats,
        "x_var": x_var,
        "y_var": y_var,
        "c_var": c_var,
        "x_units": units_map.get(x_var, ""),
        "y_units": units_map.get(y_var, ""),
        "c_units": units_map.get(c_var, "") if c_var else "",
    }

    if binary:
        # x as f64 keeps datetime epoch-ms exact (and value-axis precision); y/c as
        # f32 match the float32 source. qc_pass / in_selection ride along as bytes
        # only when populated (highlight modes), mirroring the JSON path's [] default.
        x_arr = (plot_x.astype('datetime64[ms]').astype('int64') if is_x_dt else plot_x)
        arrays = [("x", x_arr, "f64"), ("y", plot_y, "f32")]
        if plot_c is not None:
            arrays.append(("c", plot_c, "f32"))
        if apply_qc:
            arrays.append(("qc_pass", plot_qc, "u8"))
        if plot_sel is not None:
            arrays.append(("in_selection", plot_sel, "u8"))
        _mark("serialize")
        return _pack_plot_binary(meta, arrays)

    x_out = pd.to_datetime(plot_x).strftime('%Y-%m-%d %H:%M:%S').tolist() if is_x_dt else _floats_to_list(plot_x)
    y_out = _floats_to_list(plot_y)
    c_out = _floats_to_list(plot_c) if plot_c is not None else []
    _mark("serialize")
    return {
        "x": x_out,
        "y": y_out,
        "c": c_out,
        **meta,
        "qc_pass": plot_qc.tolist() if apply_qc else [],
        "in_selection": plot_sel.tolist() if plot_sel is not None else [],
    }

def get_plot_data_bounds(filepath, x_var, y_var, c_var="", apply_qc=False, qc_flags="1,2,5,8",
                          highlight_qc=False, filter_time=True,
                          x_min=None, x_max=None, y_min=None, y_max=None, is_x_dt=False,
                          view_x_min=None, view_x_max=None, view_y_min=None, view_y_max=None,
                          profile_num=None, cycle_num=None, cycle_var=None, sci_phases=None, direction_filter=None,
                          ctd_interpolate=False, ctd_qc=False, highlight_profile=False):
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
    if cycle_num is not None and cycle_var and cycle_var in var_names:
        vars_to_extract.add(cycle_var)
    if sci_phases and "SCI_PHASE" in var_names:
        vars_to_extract.add("SCI_PHASE")
    if direction_filter and "PROFILE_DIRECTION" in var_names:
        vars_to_extract.add("PROFILE_DIRECTION")
    ctd_var_map = _resolve_ctd_var_map(filepath) if (ctd_interpolate or ctd_qc) else {}
    if ctd_interpolate or ctd_qc:
        for actual in ctd_var_map.values():
            vars_to_extract.add(actual)
            if f"{actual}_QC" in var_names:
                vars_to_extract.add(f"{actual}_QC")
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
            canon = _build_ctd_canonical_dict(data_dict, ctd_var_map, actual_time_var)
            processed = _apply_ctd_processing(
                canon, actual_time_var, _get_var_units(filepath),
                interpolate=ctd_interpolate, apply_ctd_qc=ctd_qc,
            )
            data_dict = {**data_dict, **_emit_overlay(processed, ctd_var_map)}

    x_vals = data_dict.get(x_var, np.array([]))
    y_vals = data_dict.get(y_var, np.array([]))
    c_vals = data_dict.get(c_var) if c_var and c_var != 'black' else None

    if len(x_vals) == 0:
        return {"error": "No data found."}

    valid_mask = ~pd.isnull(x_vals) & ~pd.isnull(y_vals)
    qc_pass_mask = np.ones(len(x_vals), dtype=bool)

    profile_mask = _apply_profile_mask(data_dict, profile_num)
    cycle_mask = _apply_cycle_mask(data_dict, cycle_num, cycle_var)
    phase_mask = _apply_phase_mask(data_dict, sci_phases)
    dir_mask = _apply_direction_mask(data_dict, direction_filter)

    selection_mask = None
    for m in (profile_mask, cycle_mask, phase_mask, dir_mask):
        if m is not None:
            selection_mask = m if selection_mask is None else (selection_mask & m)

    # In highlight mode keep all points (the selection is returned per-point);
    # otherwise subset to the selection as normal.
    if not highlight_profile and selection_mask is not None:
        valid_mask &= selection_mask

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
    plot_sel = selection_mask[valid_mask] if (highlight_profile and selection_mask is not None) else None

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
        if plot_sel is not None:
            plot_sel = plot_sel[bounds_mask]

    total = len(plot_x)
    if total == 0:
        return {"error": "No points in view."}

    # The fetch deliberately over-fetches a padded margin so edge points render,
    # so `total` overcounts what's actually visible. When the exact (unpadded)
    # view bounds are supplied, count only the points inside them for an honest
    # "Points in view" stat; otherwise fall back to total.
    plotted = total
    if view_x_min is not None and view_x_max is not None:
        if np.issubdtype(plot_x.dtype, np.datetime64):
            vx_min = np.datetime64(pd.to_datetime(view_x_min, unit='ms'))
            vx_max = np.datetime64(pd.to_datetime(view_x_max, unit='ms'))
            view_mask = (plot_x >= vx_min) & (plot_x <= vx_max)
        else:
            view_mask = (plot_x.astype(float) >= float(view_x_min)) & (plot_x.astype(float) <= float(view_x_max))
        if view_y_min is not None and view_y_max is not None:
            view_mask &= (plot_y >= float(view_y_min)) & (plot_y <= float(view_y_max))
        plotted = int(view_mask.sum())

    is_x_dt = np.issubdtype(plot_x.dtype, np.datetime64)

    if total > MAX_RENDER_POINTS:
        step = total // MAX_RENDER_POINTS
        plot_x = plot_x[::step]
        plot_y = plot_y[::step]
        plot_qc = plot_qc[::step]
        if plot_c is not None:
            plot_c = plot_c[::step]
        if plot_sel is not None:
            plot_sel = plot_sel[::step]

    x_out = pd.to_datetime(plot_x).strftime('%Y-%m-%d %H:%M:%S').tolist() if is_x_dt else _floats_to_list(plot_x)
    y_out = _floats_to_list(plot_y)

    c_out, c_min, c_max = [], 0.0, 1.0
    if plot_c is not None:
        c_out = _floats_to_list(plot_c)
        valid_c = plot_c[plot_qc] if apply_qc else plot_c
        if len(valid_c) > 0:
            c_min = float(np.nanpercentile(valid_c, 0.1))
            c_max = float(np.nanpercentile(valid_c, 99.9))

    units_map = _get_var_units(filepath)
    return {
        "x": x_out, "y": y_out, "c": c_out, "is_x_dt": bool(is_x_dt),
        "c_min": c_min, "c_max": c_max, "qc_applied": apply_qc,
        "qc_pass": plot_qc.tolist() if apply_qc else [],
        "profile_highlight": bool(highlight_profile and selection_mask is not None),
        "in_selection": plot_sel.tolist() if plot_sel is not None else [],
        "x_var": x_var, "y_var": y_var, "c_var": c_var,
        "x_units": units_map.get(x_var, ""),
        "y_units": units_map.get(y_var, ""),
        "c_units": units_map.get(c_var, "") if c_var else "",
        # Count of valid points within the zoomed view (pre-downsample), so the
        # stats card can show "Points in view" while the user is zoomed in.
        "plotted": int(plotted)
    }