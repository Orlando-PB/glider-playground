"""Derive CTD variables (practical/absolute salinity, conservative temperature,
density) from conductivity using the TEOS-10 / GSW toolbox, and derive scientific
phases and profile numbers from depth.

Run once per file during processing, AFTER preload (see cache_logic). 
Results are written to the per-file derived store (plot_logic) so they appear 
and behave like native variables everywhere.
"""

import re
import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from . import plot_logic
from . import spatial_logic

try:
    import gsw
    _HAS_GSW = True
except Exception:  # pragma: no cover - import guard
    _HAS_GSW = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CALC = "Derived (not in original file) — "

# Profile detection parameters. These are the hardcoded defaults of the
# pelagos_py "Find Profiles" pipeline step, ported here verbatim so the
# playground classifies profiles identically.
PROF_TIME_WINDOW_SEC = 30        # resample / smoothing window (seconds)
PROF_VELOCITY_THRESH = 0.033     # |vertical velocity| (m/s) for ascent/descent
PROF_ACCEL_THRESH = 0.0005       # max |acceleration| for a stable transect
PROF_TRANSITION_BUFFER_SEC = 30  # trimmed off each end of a phase block
PROF_MIN_DURATION_MINS = 5       # minimum minutes for a phase block to count
PROF_PEAK_PROMINENCE = 20        # prominence (depth units) for an inflection
PROF_MIN_PEAK_DIST = 20          # minimum bins between inflection peaks
PROF_GAP_THRESHOLD_MINS = 5      # time gap that splits the record into chunks
PROF_SURFACE_DEPTH = 20          # depth below which a chunk extreme is a peak
PROF_SURFACING_THRESHOLD = 5     # depth below which a turn is reclassed surfacing
PROF_PARKING_GRADIENT = 0.005    # |gradient| (m/s) reverting parking to asc/desc

# CF-ish metadata for each derived variable.
DERIVED_METADATA = {
    "PRAC_SALINITY": {"units": "1", "description": _CALC + "Practical salinity, derived from conductivity via TEOS-10/GSW"},
    "ABS_SALINITY": {"units": "g/kg", "description": _CALC + "Absolute salinity, derived via TEOS-10/GSW"},
    "CONS_TEMP": {"units": "degrees_Celsius", "description": _CALC + "Conservative temperature, derived via TEOS-10/GSW"},
    "DENSITY": {"units": "kg/m3", "description": _CALC + "In-situ density, derived via TEOS-10/GSW"},
    "SCI_PHASE": {"units": "1", "description": _CALC + "Scientific phase classification (0 unknown, 1 ascent, 2 descent, 3 surfacing, 4 parking, 5 inflection, 6 propelled, 7 transition)"},
    "PROFILE_NUMBER": {"units": "1", "description": _CALC + "Derived profile number (NaN = no profile, e.g. surfacing)"},
    "PROFILE_DIRECTION": {"units": "1", "description": _CALC + "Profile direction (-1=ascent, 1=descent, 0=transect, NaN otherwise)"},
    "CYCLE": {"units": "1", "description": _CALC + "Continuous cycle number derived from surfacing-to-descent transitions"},
    "PROFILE_GRADIENT": {"units": "m/s", "description": _CALC + "Per-profile vertical depth gradient (linear fit over ascent/descent core)"},
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _interp_over_time(arr, time_vals):
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

def provided(n, existing):
    return n in existing or (n + "_ADJUSTED") in existing

# ---------------------------------------------------------------------------
# Core Computations
# ---------------------------------------------------------------------------

def _compute_ctd(filepath, log, names, existing, time_var):
    wanted = [n for n in ("PRAC_SALINITY", "ABS_SALINITY", "CONS_TEMP", "DENSITY") if not provided(n, existing)]
    if not wanted or not _HAS_GSW:
        return [], {}, {}

    var_map = plot_logic._resolve_ctd_var_map(filepath)
    if not all(k in var_map for k in ("CNDC", "TEMP", "PRES")):
        return [], {}, {}

    lat_name, lon_name = spatial_logic._resolve_latlon_names(names)
    if not lat_name or not lon_name:
        log("No LATITUDE/LONGITUDE - skipping CTD derivation")
        return [], {}, {}

    needed = set(var_map.values())
    for c in ("CNDC", "TEMP", "PRES"):
        needed.add(var_map[c] + "_QC")
    needed.update([lat_name, lon_name])
    if time_var:
        needed.add(time_var)
        
    data = plot_logic._read_vars_cached(filepath, tuple(sorted(needed)))
    if not data:
        return [], {}, {}

    log("CTD derive: cleaning conductivity / temperature / pressure")
    canon = plot_logic._build_ctd_canonical_dict(data, var_map, time_var)
    cleaned = plot_logic._apply_ctd_processing(
        canon, time_var, plot_logic._get_var_units(filepath),
        interpolate=True, apply_ctd_qc=True,
    )

    try:
        cndc = np.asarray(cleaned["CNDC"], dtype=float)
        temp = np.asarray(cleaned["TEMP"], dtype=float)
        pres = np.asarray(cleaned["PRES"], dtype=float)
        lat = np.asarray(data[lat_name], dtype=float)
        lon = np.asarray(data[lon_name], dtype=float)
    except Exception as e:
        log(f"CTD derive: input read failed ({e})")
        return [], {}, {}

    tvals = data.get(time_var) if time_var else None
    cndc = _interp_over_time(cndc, tvals)
    temp = _interp_over_time(temp, tvals)
    pres = _interp_over_time(pres, tvals)

    n = len(pres)
    def _fit(arr):
        if arr.ndim == 0 or arr.size == 1:
            return np.full(n, float(arr.reshape(-1)[0]) if arr.size else np.nan)
        return arr
    lat, lon = _fit(lat), _fit(lon)

    # GPS fixes are surface-only and sparse, so lat/lon are NaN at virtually every
    # CTD sample row. SA_from_SP needs a position at each row, so without this the
    # GSW outputs (ABS_SALINITY/CONS_TEMP/DENSITY) only land on the GPS rows —
    # disjoint from where TEMP/PRES actually have data — and any plot combining a
    # derived var with a raw one yields zero points. Carry position to the CTD rows
    # by the same time interpolation already used for CNDC/TEMP/PRES (position
    # varies slowly, so a time-linear fill is well within GPS error).
    lat = _interp_over_time(lat, tvals)
    lon = _interp_over_time(lon, tvals)

    if not (len(cndc) == len(temp) == n == len(lat) == len(lon)):
        log("CTD derive: input length mismatch - skipping")
        return [], {}, {}

    log(f"CTD derive: computing {', '.join(wanted)} via GSW")
    try:
        sp = gsw.SP_from_C(cndc, temp, pres)
        sa = gsw.SA_from_SP(sp, pres, lon, lat)
        ct = gsw.CT_from_t(sa, temp, pres)
        rho = gsw.rho(sa, ct, pres)
    except Exception as e:
        log(f"CTD derive: GSW computation failed ({e})")
        return [], {}, {}

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
        
        qc_name = name + "_QC"
        if not provided(qc_name, existing) and qc_name not in existing:
            arrays[qc_name] = np.where(np.isfinite(arr), 1, 9).astype(np.int8)
            meta[qc_name] = {
                "units": "1", "type": "numeric",
                "description": f"Quality flag for {name} (1=good, 9=missing; derived)",
            }

    return wanted, arrays, meta


def _classify_profiles(df_raw, depth_col, target_transect_phase):
    """Port of the pelagos_py "Find Profiles" classifier (hardcoded defaults).

    Takes a raw frame with ``TIME``, ``depth_col`` and an ``ORIG_IDX`` column and
    returns a per-measurement frame carrying ``SCI_PHASE``, ``PROFILE_DIRECTION``,
    ``PROFILE_NUMBER``, ``CYCLE`` and ``GRADIENT`` (plus the original columns),
    or ``None`` if there isn't enough data to classify. Results stay on the raw
    measurement axis; ``ORIG_IDX`` lets the caller scatter them back to file order.
    """
    # --- Clean & resample to the analysis grid -----------------------------
    df = df_raw.dropna(subset=["TIME", depth_col]).sort_values("TIME")
    df = df[df[depth_col] != 0].copy()
    df = df.drop_duplicates(subset=["TIME"]).reset_index(drop=True)
    if len(df) < 2:
        return None

    window_str = f"{PROF_TIME_WINDOW_SEC}s"
    df = df.set_index("TIME").resample(window_str).mean().dropna(subset=[depth_col])
    df.reset_index(inplace=True)
    if len(df) < 2:
        return None

    # Seconds since the first bin. Relative (not epoch) seconds keep this correct
    # regardless of the datetime resolution (ns vs us) — only differences are used
    # (np.gradient, block durations, trims), so the offset is immaterial.
    time_seconds = (df["TIME"] - df["TIME"].iloc[0]).dt.total_seconds().to_numpy()
    depth = df[depth_col].values

    # --- Smoothed vertical velocity & acceleration -------------------------
    df.set_index("TIME", inplace=True)
    smoothed_depth = df[depth_col].rolling(window_str, center=True, min_periods=1).mean().values
    raw_velocity = np.gradient(smoothed_depth, time_seconds)
    despiked_velocity = pd.Series(raw_velocity, index=df.index).rolling(window_str, center=True, min_periods=1).median()
    smoothed_velocity = despiked_velocity.rolling("15s", center=True, min_periods=1).mean().values
    raw_acceleration = np.gradient(smoothed_velocity, time_seconds)
    despiked_acceleration = pd.Series(raw_acceleration, index=df.index).rolling(window_str, center=True, min_periods=1).median()
    smoothed_acceleration = despiked_acceleration.rolling("15s", center=True, min_periods=1).mean().values
    df.reset_index(inplace=True)

    # --- Raw phase from velocity (asc/desc) and acceleration (transect) ----
    raw_phases = np.zeros(len(df), dtype=int)
    raw_phases[smoothed_velocity > PROF_VELOCITY_THRESH] = 2
    raw_phases[smoothed_velocity < -PROF_VELOCITY_THRESH] = 1
    transect_mask = (raw_phases == 0) & (np.abs(smoothed_acceleration) <= PROF_ACCEL_THRESH)
    raw_phases[transect_mask] = target_transect_phase

    # --- Keep only blocks longer than the minimum duration, trimmed at ends -
    phases = np.zeros(len(df), dtype=int)
    for p_val in [1, 2, target_transect_phase]:
        mask = (raw_phases == p_val)
        padded = np.concatenate(([False], mask, [False]))
        starts = np.where(padded[1:] & ~padded[:-1])[0]
        ends = np.where(~padded[1:] & padded[:-1])[0]

        for s, e in zip(starts, ends):
            start_time = time_seconds[s]
            end_time = time_seconds[e - 1]
            block_duration = end_time - start_time

            if block_duration < (PROF_MIN_DURATION_MINS * 60):
                continue

            actual_trim = min(PROF_TRANSITION_BUFFER_SEC, block_duration / 3)

            trim_s = s
            while trim_s < e and (time_seconds[trim_s] - start_time) <= actual_trim:
                trim_s += 1

            trim_e = e - 1
            while trim_e >= s and (end_time - time_seconds[trim_e]) <= actual_trim:
                trim_e -= 1

            if trim_s <= trim_e:
                phases[trim_s:trim_e + 1] = p_val

    # --- Inflection detection: depth peaks + gap-chunk extremes ------------
    deep_peaks, _ = find_peaks(depth, prominence=PROF_PEAK_PROMINENCE, distance=PROF_MIN_PEAK_DIST)
    shallow_peaks, _ = find_peaks(-depth, prominence=PROF_PEAK_PROMINENCE, distance=PROF_MIN_PEAK_DIST)

    gap_mask = df["TIME"].diff() > pd.Timedelta(minutes=PROF_GAP_THRESHOLD_MINS)
    chunk_ids = gap_mask.cumsum()

    extra_peaks = []
    for _, chunk in df.groupby(chunk_ids):
        if chunk.empty:
            continue
        min_idx = chunk[depth_col].idxmin()
        if chunk.loc[min_idx, depth_col] <= PROF_SURFACE_DEPTH:
            extra_peaks.append(min_idx)
        max_idx = chunk[depth_col].idxmax()
        if chunk.loc[max_idx, depth_col] > PROF_SURFACE_DEPTH:
            extra_peaks.append(max_idx)

    all_peaks = np.unique(np.concatenate((deep_peaks, shallow_peaks, extra_peaks))).astype(int)
    valid_peaks = [p for p in all_peaks if phases[p] != target_transect_phase]

    # --- Inflections at the asc/desc edges of each transect block ----------
    transect_inflections = []
    padded_t = np.concatenate(([False], phases == target_transect_phase, [False]))
    t_starts = np.where(padded_t[1:] & ~padded_t[:-1])[0]
    t_ends = np.where(~padded_t[1:] & padded_t[:-1])[0] - 1

    for s, e in zip(t_starts, t_ends):
        idx = s - 1
        while idx >= 0 and phases[idx] not in [1, 2]:
            idx -= 1
        if idx >= 0:
            gap = depth[idx:s + 1]
            infl_idx = idx + (np.argmax(gap) if phases[idx] == 2 else np.argmin(gap))
            transect_inflections.append(infl_idx)

        idx = e + 1
        while idx < len(phases) and phases[idx] not in [1, 2]:
            idx += 1
        if idx < len(phases):
            gap = depth[e:idx + 1]
            infl_idx = e + (np.argmax(gap) if phases[idx] == 1 else np.argmin(gap))
            transect_inflections.append(infl_idx)

    all_inflections = np.unique(np.concatenate((valid_peaks, transect_inflections))).astype(int)
    phases[all_inflections] = 5

    # --- Surfacing: shallow inflection/transect points become phase 3 ------
    shallow_mask = (depth <= PROF_SURFACING_THRESHOLD) & (np.isin(phases, [5, target_transect_phase]))
    phases[shallow_mask] = 3

    # --- Fill ambiguous zero blocks: same-on-both-sides, else transition ---
    padded_zeros = np.concatenate(([False], phases == 0, [False]))
    zero_starts = np.where(padded_zeros[1:] & ~padded_zeros[:-1])[0]
    zero_ends = np.where(~padded_zeros[1:] & padded_zeros[:-1])[0] - 1

    for s, e in zip(zero_starts, zero_ends):
        left_val = phases[s - 1] if s > 0 else None
        right_val = phases[e + 1] if e < len(phases) - 1 else None
        if left_val is not None and right_val is not None:
            phases[s:e + 1] = left_val if left_val == right_val else 7
        else:
            phases[s:e + 1] = 7

    # --- Drifting parking/propelled blocks revert to ascent/descent --------
    parking_mask = np.isin(phases, [4, 6])
    padded_parking = np.concatenate(([False], parking_mask, [False]))
    p_starts = np.where(padded_parking[1:] & ~padded_parking[:-1])[0]
    p_ends = np.where(~padded_parking[1:] & padded_parking[:-1])[0]

    for s, e in zip(p_starts, p_ends):
        if (e - s) < 2:
            continue
        t_blk = time_seconds[s:e]
        m, _ = np.polyfit(t_blk - t_blk[0], depth[s:e], 1)
        if abs(m) > PROF_PARKING_GRADIENT:
            phases[s:e] = 2 if m > 0 else 1

    df["PHASE"] = phases

    # --- Map binned phases back onto every raw measurement -----------------
    df_merge = df[["TIME", "PHASE"]].copy()
    df_merge["BIN_TIME"] = df_merge["TIME"]
    mapped_df = pd.merge_asof(
        df_raw.dropna(subset=["TIME"]).sort_values("TIME"),
        df_merge.sort_values("TIME"),
        on="TIME",
        direction="nearest",
    )
    mapped_df["PHASE"] = mapped_df["PHASE"].fillna(7).astype(int)

    # An inflection bin spans several raw points; demote them all to transition,
    # then re-flag only the single most extreme raw point in each as inflection.
    inflection_times = df.loc[df["PHASE"] == 5, "TIME"]
    mapped_df.loc[mapped_df["PHASE"] == 5, "PHASE"] = 7
    for t in inflection_times:
        mapped_mask = mapped_df["BIN_TIME"] == t
        if not mapped_mask.any():
            continue
        idx = df.index[df["TIME"] == t][0]
        curr_d = df.loc[idx, depth_col]
        d_prev = df.loc[idx - 1, depth_col] if idx > 0 else curr_d
        d_next = df.loc[idx + 1, depth_col] if idx < len(df) - 1 else curr_d
        raw_subset = mapped_df[mapped_mask]
        if curr_d >= (d_prev + d_next) / 2:
            extreme_idx = raw_subset[depth_col].idxmax()
        else:
            extreme_idx = raw_subset[depth_col].idxmin()
        mapped_df.loc[extreme_idx, "PHASE"] = 5

    mapped_df.drop(columns=["BIN_TIME"], inplace=True)
    mapped_df["SCI_PHASE"] = mapped_df["PHASE"]

    # --- Direction -----------------------------------------------------------
    phases_arr = mapped_df["SCI_PHASE"].to_numpy()
    n = len(phases_arr)

    direction = np.full(n, np.nan)
    direction[phases_arr == 1] = -1
    direction[phases_arr == 2] = 1
    direction[np.isin(phases_arr, [3, 4, 6])] = 0
    mapped_df["PROFILE_DIRECTION"] = direction

    # --- Profile number: each asc/desc core, extended to its boundaries ----
    core_mask = np.isin(phases_arr, [1, 2])
    padded_core = np.concatenate(([False], core_mask, [False]))
    c_starts = np.where(padded_core[1:] & ~padded_core[:-1])[0]
    c_ends = np.where(~padded_core[1:] & padded_core[:-1])[0]  # exclusive
    core_blocks = list(zip(c_starts, c_ends))

    profile_num = np.full(n, np.nan)
    if core_blocks:
        boundaries = []  # inclusive last-index of each profile (except the last)
        for i in range(len(core_blocks) - 1):
            end_i = core_blocks[i][1]
            start_next = core_blocks[i + 1][0]
            if start_next <= end_i:
                boundaries.append(end_i - 1)
                continue

            region = np.arange(end_i, start_next)
            region_phases = phases_arr[region]
            infl = region[region_phases == 5]
            surf = region[region_phases == 3]
            if len(infl) > 0:
                split = int(infl[-1])
            elif len(surf) > 0:
                split = int(surf[-1])
            else:
                split = (end_i + start_next - 1) // 2
            boundaries.append(split)

        prev_end = 0
        for k in range(len(core_blocks)):
            this_end = boundaries[k] + 1 if k < len(boundaries) else n
            profile_num[prev_end:this_end] = k + 1
            prev_end = this_end

    # Surfacing rows belong to the cycle but not to any profile.
    profile_num[phases_arr == 3] = np.nan
    mapped_df["PROFILE_NUMBER"] = profile_num

    # --- Cycle: increments on each surfacing -> descent transition ---------
    surf_mask = mapped_df["SCI_PHASE"] == 3
    down_mask = mapped_df["SCI_PHASE"] == 2
    state_subset = mapped_df.loc[surf_mask | down_mask]
    is_new_cycle = (state_subset["SCI_PHASE"] == 2) & (state_subset["SCI_PHASE"].shift(1) == 3)
    cycle_trigger = pd.Series(0, index=mapped_df.index)
    cycle_trigger.loc[state_subset[is_new_cycle].index] = 1
    mapped_df["CYCLE"] = cycle_trigger.cumsum() + 1

    # --- Gradient: per-profile linear depth-vs-time fit over the core rows --
    mapped_df["GRADIENT"] = np.nan
    core_series = pd.Series(core_mask, index=mapped_df.index)
    core_rows = mapped_df[core_series & mapped_df["PROFILE_NUMBER"].notna()]
    for _, group in core_rows.groupby("PROFILE_NUMBER"):
        x = (group["TIME"] - group["TIME"].iloc[0]).dt.total_seconds().values
        y = group[depth_col].values
        if len(x) > 1:
            m, _ = np.polyfit(x, y, 1)
            pnum = group["PROFILE_NUMBER"].iloc[0]
            mapped_df.loc[mapped_df["PROFILE_NUMBER"] == pnum, "GRADIENT"] = m

    return mapped_df


def _compute_profiles(filepath, log, names, existing, time_var):
    wanted = [n for n in ("SCI_PHASE", "PROFILE_NUMBER", "PROFILE_DIRECTION", "CYCLE", "PROFILE_GRADIENT") if n not in existing]
    if not wanted or not time_var:
        return [], {}, {}

    var_map = plot_logic._resolve_ctd_var_map(filepath)
    if "PRES" not in var_map:
        log("No PRES - skipping profile derivation")
        return [], {}, {}

    data = plot_logic._read_vars_cached(filepath, (var_map["PRES"], time_var))
    if not data:
        return [], {}, {}

    log("Profile derive: classifying phases, profiles and cycles")
    t_arr = np.asarray(data[time_var])
    pres_arr = np.asarray(data[var_map["PRES"]], dtype=float)
    t_parsed = pd.to_datetime(t_arr, errors="coerce")

    n_orig = len(pres_arr)
    if n_orig < 2:
        return [], {}, {}

    # ALR-class platforms are propelled (transect phase 6); everything else parks (4).
    target_transect = 6 if "ALR" in str(filepath).upper() else 4

    df_raw = pd.DataFrame({"TIME": t_parsed, "PRES": pres_arr})
    df_raw["ORIG_IDX"] = np.arange(n_orig)

    mapped_df = _classify_profiles(df_raw, "PRES", target_transect)
    if mapped_df is None or mapped_df.empty:
        return [], {}, {}

    # Scatter the per-measurement results back onto the original file axis.
    valid_idx = mapped_df["ORIG_IDX"].to_numpy().astype(int)
    out = {
        "SCI_PHASE": np.zeros(n_orig, dtype=int),
        "PROFILE_NUMBER": np.full(n_orig, np.nan),
        "PROFILE_DIRECTION": np.full(n_orig, np.nan),
        "CYCLE": np.full(n_orig, np.nan),
        "PROFILE_GRADIENT": np.full(n_orig, np.nan),
    }
    out["SCI_PHASE"][valid_idx] = mapped_df["SCI_PHASE"].to_numpy()
    out["PROFILE_NUMBER"][valid_idx] = mapped_df["PROFILE_NUMBER"].to_numpy()
    out["PROFILE_DIRECTION"][valid_idx] = mapped_df["PROFILE_DIRECTION"].to_numpy()
    out["CYCLE"][valid_idx] = mapped_df["CYCLE"].to_numpy()
    out["PROFILE_GRADIENT"][valid_idx] = mapped_df["GRADIENT"].to_numpy()

    arrays, meta = {}, {}
    mapped_mask = np.isin(np.arange(n_orig), valid_idx)
    for name in wanted:
        arrays[name] = out[name]
        meta[name] = {**DERIVED_METADATA[name], "type": "numeric"}

        qc_name = name + "_QC"
        if qc_name not in existing:
            arrays[qc_name] = np.where(mapped_mask, 1, 9).astype(np.int8)
            meta[qc_name] = {
                "units": "1", "type": "numeric",
                "description": f"Quality flag for {name} (1=good, 9=missing; derived)",
            }

    return wanted, arrays, meta


def _compute_backscatter(filepath, log, names, existing, time_var):
    """Derive a simple, visual-only particulate backscatter (BBP) from raw beta.

    Only runs when no backscatter product is already present (native or from an
    upstream pipeline). The conversion is deliberately crude — ``bbp = 2*pi*chi*beta``
    — skipping the seawater-scattering subtraction and temperature/salinity
    correction of the full science pipeline. That keeps it fast and dependency-free;
    it's meant for a visual feel of the signal, not science-grade numbers. A
    rolling-median baseline and the residual "spikes" are also produced, since the
    despiked view is what makes backscatter features (particle bursts) legible.
    """
    # Skip if any backscatter product already exists (don't override real data).
    if any(n.upper().startswith("BBP") for n in existing):
        return [], {}, {}

    # Find a raw beta variable (e.g. BETA_BACKSCATTERING700). The wavelength digits
    # may differ; if there are several, any one is fine — take the first.
    beta_var = next((n for n in names
                     if "BETA" in n.upper() and "BACKSCATTER" in n.upper()
                     and not n.upper().endswith("_QC")), None)
    if not beta_var:
        return [], {}, {}

    data = plot_logic._read_vars_cached(filepath, (beta_var,))
    if not data or beta_var not in data:
        return [], {}, {}

    beta = np.asarray(data[beta_var], dtype=float)
    if beta.size == 0 or not np.any(np.isfinite(beta)):
        return [], {}, {}

    # Name the output after the sensor wavelength when present (else BBP700).
    m = re.search(r"(\d{3,4})", beta_var)
    base = f"BBP{m.group(1)}" if m else "BBP700"

    CHI = 1.076                         # chi factor for a ~124° backscatter sensor
    bbp = 2.0 * np.pi * CHI * beta      # m-1.sr-1 * sr -> m-1

    # Despike: sample-count rolling median baseline, spikes = signal - baseline.
    s = pd.Series(bbp)
    baseline = s.rolling(50, center=True, min_periods=1).median().to_numpy()
    spikes = bbp - baseline

    log(f"Backscatter derive: {beta_var} -> {base} (+ baseline/spikes)")

    outputs = {
        base: (bbp, f"Particulate backscatter (simple 2πχβ from {beta_var}; visual only, not science-grade)"),
        f"{base}_BASELINE": (baseline, f"Rolling-median baseline of {base} (window 50 samples)"),
        f"{base}_SPIKES": (spikes, f"{base} minus its baseline — particulate backscatter spikes"),
    }

    wanted, arrays, meta = [], {}, {}
    for name, (arr, desc) in outputs.items():
        if name in existing:
            continue
        arr = np.asarray(arr, dtype=float)
        arrays[name] = arr
        meta[name] = {"units": "m-1", "type": "numeric", "description": _CALC + desc}
        wanted.append(name)

        qc_name = name + "_QC"
        if qc_name not in existing:
            arrays[qc_name] = np.where(np.isfinite(arr), 1, 9).astype(np.int8)
            meta[qc_name] = {
                "units": "1", "type": "numeric",
                "description": f"Quality flag for {name} (1=good, 9=missing; derived)",
            }

    return wanted, arrays, meta


# ---------------------------------------------------------------------------
# Master Entry Point
# ---------------------------------------------------------------------------

def derive_all_extra_variables(filepath, log_cb=None):
    """Entry point to calculate all CTD and profile variables at once, 
    saving to the derived store in a single operation."""
    def log(msg):
        if log_cb:
            try: log_cb(msg)
            except Exception: pass

    names = list(plot_logic._get_var_names(filepath) or [])
    if not names:
        return []
        
    existing = set(names)
    time_var = _resolve_time_var(names)
    
    all_wanted = []
    all_arrays = {}
    all_meta = {}

    try:
        cw, ca, cm = _compute_ctd(filepath, log, names, existing, time_var)
        all_wanted.extend(cw)
        all_arrays.update(ca)
        all_meta.update(cm)
    except Exception as e:
        log(f"CTD derivation failed ({e})")

    try:
        pw, pa, pm = _compute_profiles(filepath, log, names, existing, time_var)
        all_wanted.extend(pw)
        all_arrays.update(pa)
        all_meta.update(pm)
    except Exception as e:
        log(f"Profile derivation failed ({e})")

    try:
        bw, ba, bm = _compute_backscatter(filepath, log, names, existing, time_var)
        all_wanted.extend(bw)
        all_arrays.update(ba)
        all_meta.update(bm)
    except Exception as e:
        log(f"Backscatter derivation failed ({e})")

    if all_arrays:
        plot_logic.save_derived(filepath, all_arrays, all_meta)
        log(f"Saved derived variables: {', '.join(all_wanted)}")

    return all_wanted