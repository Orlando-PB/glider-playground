"""Derive CTD variables (practical/absolute salinity, conservative temperature,
density) from conductivity using the TEOS-10 / GSW toolbox, derive scientific
phases and profile numbers from depth, and derive a TIME QC flag (NaT/bad-order/
out-of-range) so TIME can be filtered by the QC flag chips like any other var.

Run once per file during processing, AFTER preload (see cache_logic). 
Results are written to the per-file derived store (plot_logic) so they appear 
and behave like native variables everywhere.
"""

import re
import numpy as np
import pandas as pd

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
PROF_SMOOTHING_WINDOW_SEC = 30        # rolling-mean window (seconds) applied to depth
PROF_VELOCITY_THRESH = 0.033          # |vertical velocity| (depth units/s) for ascent/descent
PROF_MIN_DURATION_SEC = 60            # minimum seconds for an ascent/descent run to be trusted
PROF_GAP_THRESHOLD_MINS = 5           # time gap that splits the record into disconnected chunks
PROF_SURFACING_DEPTH_THRESHOLD = 2.0  # depth below which a turn/propelled run is surfacing
PROF_MIN_TRANSECT_DURATION_SEC = 300  # minimum duration for an unknown run to be propelled

_UNKNOWN = 0
_ASCENT = 1
_DESCENT = 2
_SURFACING = 3
_PARKING = 4
_INFLECTION = 5
_PROPELLED = 6
_TRANSITION = 7

_PROF_DERIVED_COLUMNS = ["SCI_PHASE", "PROFILE_NUMBER", "PROFILE_DIRECTION", "CYCLE", "GRADIENT"]

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

def _compute_time_qc(filepath, log, names, existing, time_var):
    """Derive a QC flag for TIME once per file, so TIME can be filtered by the
    same QC-flag chips as everything else instead of a separate "Filter Time"
    toggle: NaT -> 9 (missing), a timestamp that runs backwards relative to
    everything before it -> 4 (bad), pre-1990/future -> 4 (bad), else -> 1
    (good). Only runs if the file has no native <time_var>_QC.

    NaT and non-monotonic rows are ALSO always hard-dropped by the plot
    pipeline regardless of this flag (see plot_logic._hard_time_valid_mask) —
    that drop is unconditional (not just "bad" data, not meaningful data at
    all), so the flag value assigned to them here is for visibility/counting
    only, not gating.
    """
    qc_name = f"{time_var}_QC" if time_var else None
    if not time_var or not qc_name or qc_name in existing:
        return [], {}, {}

    data = plot_logic._read_vars_cached(filepath, (time_var,))
    if not data or time_var not in data:
        return [], {}, {}

    t = pd.to_datetime(np.asarray(data[time_var]), errors="coerce")
    n = len(t)
    if n == 0:
        return [], {}, {}
    qc = np.ones(n, dtype=np.int8)

    nat_mask = np.asarray(t.isna())
    qc[nat_mask] = 9

    valid = ~nat_mask
    min_time = np.datetime64(pd.Timestamp("1990-01-01"))
    # TIME is naive UTC (see module docstring) — comparing it against
    # pd.Timestamp.now() (naive LOCAL walltime) silently shifted the "is this
    # timestamp in the future" cutoff by the server's UTC offset, so on a
    # machine behind UTC the most recent hours of genuinely-valid live data
    # could get flagged QC=4 ("bad") and then hard-excluded by the default
    # QC-flag filter (which excludes 4) everywhere TIME is plotted.
    now_time = np.datetime64(pd.Timestamp.utcnow().tz_localize(None))
    t_vals = t.to_numpy()
    with np.errstate(invalid="ignore"):
        out_of_range = valid & ((t_vals < min_time) | (t_vals > now_time))
    qc[out_of_range] = 4

    INT_MIN = np.iinfo(np.int64).min
    t_int = t.asi8.copy()
    safe = np.where(valid, t_int, INT_MIN)
    running_max = np.maximum.accumulate(safe)
    prev_max = np.empty(n, dtype=np.int64)
    prev_max[0] = INT_MIN
    prev_max[1:] = running_max[:-1]
    non_monotonic = valid & (t_int < prev_max)
    qc[non_monotonic] = 4

    meta = {qc_name: {
        "units": "1", "type": "numeric",
        "description": _CALC + "Derived TIME QC (1=good, 4=bad [out-of-range or non-monotonic], 9=missing/NaT)",
    }}
    return [qc_name], {qc_name: qc}, meta


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
    units_map = plot_logic._get_var_units(filepath)
    canon = plot_logic._build_ctd_canonical_dict(data, var_map, time_var)
    cleaned = plot_logic._apply_ctd_processing(
        canon, time_var, units_map,
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

    # GSW's SP_from_C expects conductivity in mS/cm. Source files store CNDC in
    # S/m (sometimes labelled with the old synonym "mhos/m") rather than mS/cm,
    # so convert here based on the file's actual units — this conversion is only
    # for the salinity/density calculation below, it never touches the CNDC
    # values shown in the plot or written by "Clean".
    cndc_units = str((units_map or {}).get("CNDC", "")).strip().lower()
    if cndc_units not in plot_logic.CTD_CNDC_MSCM_UNITS:
        cndc = cndc * 10.0

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


def _prof_compute_chunk_id(time_seconds, gap_threshold_seconds):
    # Real data gaps split the record into disconnected chunks - velocity is
    # never computed, and no run ever allowed, across one.
    return np.concatenate((
        [0], np.cumsum(np.diff(time_seconds) > gap_threshold_seconds)
    )).astype(np.int32)


def _prof_gradient_per_chunk(values, time_seconds, chunk_id):
    # np.gradient over the whole record would bridge real data gaps (e.g. surface
    # comms windows, or an upcast-only glider whose data just stops mid-ascent),
    # diluting the slope right at the edge of a gap. Compute it chunk-by-chunk.
    result = np.zeros(len(values))
    for cid in np.unique(chunk_id):
        idx = np.flatnonzero(chunk_id == cid)
        if idx.size >= 2:
            result[idx] = np.gradient(values[idx], time_seconds[idx])
    return result


def _prof_smoothed_velocity(depth, time, time_seconds, chunk_id, window):
    # Smooth depth, differentiate per-chunk, then smooth the resulting velocity
    # itself (median, to despike) - both rolling passes are time-windowed so
    # they stay meaningful under irregular sampling.
    depth_series = pd.Series(depth, index=pd.DatetimeIndex(time))
    smoothed_depth = depth_series.rolling(window, center=True, min_periods=1).mean().to_numpy()
    velocity = _prof_gradient_per_chunk(smoothed_depth, time_seconds, chunk_id)
    return (
        pd.Series(velocity, index=depth_series.index)
        .rolling(window, center=True, min_periods=1)
        .median()
        .to_numpy()
    )


def _prof_runs_by_chunk(mask, chunk_id):
    # Yields (start, end) for each maximal run of True in `mask`, additionally
    # split wherever chunk_id changes inside it, so a run never bridges a gap.
    n = len(mask)
    if n == 0 or not mask.any():
        return
    boundary = np.empty(n, dtype=bool)
    boundary[0] = True
    boundary[1:] = (mask[1:] != mask[:-1]) | (chunk_id[1:] != chunk_id[:-1])
    edges = np.flatnonzero(boundary)
    edges = np.append(edges, n)
    for s, e in zip(edges[:-1], edges[1:]):
        if mask[s]:
            yield int(s), int(e)


def _prof_classify_ascent_descent(smoothed_velocity, time_seconds, chunk_id, velocity_threshold, min_duration_seconds):
    # Threshold velocity into raw ascent/descent, then run-length merge, dropping
    # runs too short to trust (sensor noise) or that straddle a chunk boundary.
    n = len(smoothed_velocity)
    raw_phase = np.zeros(n, dtype=np.int8)
    raw_phase[smoothed_velocity > velocity_threshold] = _DESCENT
    raw_phase[smoothed_velocity < -velocity_threshold] = _ASCENT

    change = np.empty(n, dtype=bool)
    change[0] = True
    change[1:] = (raw_phase[1:] != raw_phase[:-1]) | (chunk_id[1:] != chunk_id[:-1])
    run_starts = np.flatnonzero(change)
    run_ends = np.append(run_starts[1:], n)
    run_values = raw_phase[run_starts]
    run_durations = time_seconds[run_ends - 1] - time_seconds[run_starts]
    keep = (run_values != _UNKNOWN) & (run_durations >= min_duration_seconds)

    return np.repeat(np.where(keep, run_values, _UNKNOWN), run_ends - run_starts).astype(np.int8)


def _prof_classify_propelled_surfacing(phase, depth, time_seconds, chunk_id,
                                        surfacing_depth_threshold, min_duration_seconds,
                                        min_transect_duration_seconds, transect_phase):
    # Applied only to what ascent/descent left unknown. A flat, undulating stretch
    # away from the surface, gated by a much longer minimum duration than surfacing
    # so a turnaround isn't mistaken for one (a turn also sits near-zero velocity
    # briefly, but only for seconds, not minutes), is either propelled (ALR-class
    # platforms, which actually have thrusters) or parking (everything else, which
    # can only be drifting) - see `transect_phase`.
    for rs, re in _prof_runs_by_chunk(phase == _UNKNOWN, chunk_id):
        duration = time_seconds[re - 1] - time_seconds[rs]
        if np.median(depth[rs:re]) <= surfacing_depth_threshold:
            if duration >= min_duration_seconds:
                phase[rs:re] = _SURFACING
        elif duration >= min_transect_duration_seconds:
            phase[rs:re] = transect_phase


def _prof_classify_inflection(phase, depth, chunk_id, surfacing_depth_threshold):
    # The single apex of a turn between a descent and an ascent (or vice versa,
    # for a mid-water W-cast). Only the one deepest/shallowest sample is marked,
    # unless that apex itself is shallow, in which case it's surfacing instead.
    # The run's leading edge may be missing entirely (record starts mid-turn,
    # e.g. no descent ever sampled) as long as the trailing edge confirms the
    # turn; a missing trailing edge is genuinely ambiguous and always skipped.
    n = len(phase)
    for s, e in _prof_runs_by_chunk(phase == _UNKNOWN, chunk_id):
        if e == n or chunk_id[e] != chunk_id[e - 1]:
            continue
        start_gap = s == 0 or chunk_id[s - 1] != chunk_id[s]
        before = None if start_gap else phase[s - 1]
        after = phase[e]
        lo = s if start_gap else s - 1

        if after == _ASCENT and before in (_DESCENT, None):
            idx = lo + np.argmax(depth[lo:e + 1])
        elif after == _DESCENT and before in (_ASCENT, None):
            idx = lo + np.argmin(depth[lo:e + 1])
        else:
            continue
        phase[idx] = _SURFACING if depth[idx] <= surfacing_depth_threshold else _INFLECTION


def _prof_classify_transition(phase, depth, chunk_id, surfacing_depth_threshold):
    # Whatever's still unknown immediately either side of a turn - the shoulder
    # between an inflection/surfacing point and the ascent/descent it leads into
    # or out of. Shallow, it's surfacing instead, same backstop as above. Same
    # leading/trailing asymmetry as the inflection pass: the shoulder heading
    # into a turn can have nothing before it at all (the turn point itself
    # confirms it), but the shoulder coming out always needs a real ascent/descent
    # after it, or it's left unknown.
    n = len(phase)
    for s, e in _prof_runs_by_chunk(phase == _UNKNOWN, chunk_id):
        if e == n or chunk_id[e] != chunk_id[e - 1]:
            continue
        start_gap = s == 0 or chunk_id[s - 1] != chunk_id[s]
        before = None if start_gap else phase[s - 1]
        after = phase[e]
        turn_to_core = before in (_SURFACING, _INFLECTION) and after in (_ASCENT, _DESCENT)
        core_to_turn = after in (_SURFACING, _INFLECTION) and before in (_ASCENT, _DESCENT, None)
        if not (turn_to_core or core_to_turn):
            continue
        phase[s:e] = _SURFACING if np.median(depth[s:e]) <= surfacing_depth_threshold else _TRANSITION


def _prof_assign_profile_and_cycle(phase, chunk_id):
    # Each ascent/descent run is its own profile - no pairing required, so an
    # upcast with no downcast is still a valid, numbered profile. A profile also
    # claims its adjacent transition shoulders, and - only on its leading edge -
    # the single bottom inflection point that marks where it started. It never
    # reaches past surfacing/propelled/a top inflection: those always belong to
    # whatever comes after them, so a bottom turn is never claimed by both the
    # descent before it and the ascent after.
    #
    # Cycle: a new one starts as soon as a descent begins (from the same
    # extended point PROFILE_NUMBER gives it), running up to but not including
    # the start of the next descent - so it carries through the bottom
    # inflection, the ascent, its trailing transition, and surfacing, all as one
    # cycle. An ascent with nothing directly adjacent before its own extended
    # start (upcast-only) starts a fresh cycle the same way a descent would -
    # which is also what makes a mostly-propelled platform start a new cycle
    # each time it actually goes underwater.
    n = len(phase)
    core_mask = (phase == _ASCENT) | (phase == _DESCENT)
    padded = np.concatenate(([False], core_mask, [False]))
    starts = np.flatnonzero(padded[1:] & ~padded[:-1])
    ends = np.flatnonzero(~padded[1:] & padded[:-1])  # exclusive

    profile_num = np.full(n, np.nan)
    cycle = np.ones(n, dtype=np.int32)
    prev_hi = None
    current_cycle = 1
    for k, (s, e) in enumerate(zip(starts, ends), start=1):
        lo = s
        while lo > 0 and phase[lo - 1] == _TRANSITION and chunk_id[lo - 1] == chunk_id[lo]:
            lo -= 1
        if phase[s] == _ASCENT and lo > 0 and phase[lo - 1] == _INFLECTION and chunk_id[lo - 1] == chunk_id[lo]:
            lo -= 1

        hi = e
        while hi < n and phase[hi] == _TRANSITION and chunk_id[hi] == chunk_id[hi - 1]:
            hi += 1

        profile_num[lo:hi] = k

        is_new_cycle = prev_hi is None or phase[s] == _DESCENT or lo != prev_hi
        if is_new_cycle and prev_hi is not None:
            current_cycle += 1
        cycle[lo:] = current_cycle
        prev_hi = hi

    return profile_num, cycle


def _classify_profiles(df_raw, depth_col, transect_phase=_PARKING):
    """Port of the pelagos_py "Find Profiles" classifier (hardcoded defaults).

    Works directly on the raw measurement grid (no resampling/peak-finding),
    which makes it both more reliable and considerably faster than the previous
    classifier. Takes a raw frame with ``TIME``/``depth_col`` (any row order,
    NaNs allowed) and returns a frame of the same shape/order carrying
    ``SCI_PHASE``, ``PROFILE_DIRECTION``, ``PROFILE_NUMBER``, ``CYCLE`` and
    ``GRADIENT`` alongside the original columns.

    ``transect_phase``: the SCI_PHASE assigned to a long, flat, non-surface
    "unknown" stretch — ``_PROPELLED`` (6) for ALR-class platforms, which
    genuinely have thrusters, ``_PARKING`` (4) for anything else, which can
    only be drifting. Caller decides which (see ``_compute_profiles``).
    """
    df = df_raw.dropna(subset=["TIME", depth_col]).sort_values("TIME")

    if df.empty:
        out = df_raw.copy()
        out["SCI_PHASE"] = _UNKNOWN
        out["PROFILE_NUMBER"] = np.nan
        out["PROFILE_DIRECTION"] = np.nan
        out["CYCLE"] = 1
        out["GRADIENT"] = np.nan
        return out

    # astype("int64") on a datetime64 array assumes its native unit; pandas'
    # default resolution varies by version/source (ns historically, us as of
    # pandas 3), so normalize to ns first or the deltas below are silently
    # off by a unit-dependent factor (e.g. 1000x under datetime64[us]).
    time_seconds = df["TIME"].to_numpy().astype("datetime64[ns]").astype("int64") / 1e9
    depth = df[depth_col].to_numpy(dtype=float)

    chunk_id = _prof_compute_chunk_id(time_seconds, PROF_GAP_THRESHOLD_MINS * 60)
    smoothed_velocity = _prof_smoothed_velocity(
        depth, df["TIME"].to_numpy(), time_seconds, chunk_id, f"{PROF_SMOOTHING_WINDOW_SEC}s"
    )

    phase = _prof_classify_ascent_descent(
        smoothed_velocity, time_seconds, chunk_id, PROF_VELOCITY_THRESH, PROF_MIN_DURATION_SEC
    )
    _prof_classify_propelled_surfacing(
        phase, depth, time_seconds, chunk_id,
        PROF_SURFACING_DEPTH_THRESHOLD, PROF_MIN_DURATION_SEC, PROF_MIN_TRANSECT_DURATION_SEC,
        transect_phase,
    )
    _prof_classify_inflection(phase, depth, chunk_id, PROF_SURFACING_DEPTH_THRESHOLD)
    _prof_classify_transition(phase, depth, chunk_id, PROF_SURFACING_DEPTH_THRESHOLD)

    direction = np.full(len(phase), np.nan)
    direction[phase == _ASCENT] = -1
    direction[phase == _DESCENT] = 1
    direction[(phase == _SURFACING) | (phase == _PROPELLED) | (phase == _PARKING)] = 0

    profile_num, cycle = _prof_assign_profile_and_cycle(phase, chunk_id)

    result = pd.DataFrame(
        {
            "SCI_PHASE": phase,
            "PROFILE_NUMBER": profile_num,
            "PROFILE_DIRECTION": direction,
            "CYCLE": cycle,
            "GRADIENT": smoothed_velocity,
        },
        index=df.index,
    )

    out = df_raw.copy()
    out[_PROF_DERIVED_COLUMNS] = result.reindex(out.index)
    out["SCI_PHASE"] = out["SCI_PHASE"].fillna(_UNKNOWN).astype(int)
    out["CYCLE"] = out["CYCLE"].ffill().fillna(1).astype(int)
    return out


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

    # A clock reset/backward jump (NaT or a timestamp earlier than everything
    # before it) must be excluded before classification, not just sorted into
    # place: sort_values() below would otherwise interleave the reordered
    # samples with their new chronological neighbours at a near-zero time
    # delta, which sends np.gradient's velocity computation to +/-inf/NaN -
    # silently misclassifying a real (and sometimes huge) stretch of genuine
    # dives as one long "unknown" run that then gets swept into propelled/
    # parking. Same hard rule as the general plot pipeline (see
    # plot_logic._hard_time_valid_mask).
    time_ok = plot_logic._hard_time_valid_mask(t_arr)
    t_masked = np.asarray(t_parsed).copy()
    t_masked[~time_ok] = np.datetime64("NaT")

    # An exact-duplicate timestamp (e.g. two overlapping recording segments
    # after a clock reset) is just as fatal here - a zero time delta between
    # adjacent samples divides by zero in np.gradient - but isn't "backward,"
    # so the hard-drop above doesn't catch it. Keep only the first occurrence.
    # Scoped to profiling only: elsewhere (a plain scatter plot) two points
    # sharing a timestamp are harmless.
    dup = pd.Series(t_masked).duplicated(keep="first").to_numpy() & ~pd.isnull(t_masked)
    t_masked[dup] = np.datetime64("NaT")

    df_raw = pd.DataFrame({"TIME": t_masked, "PRES": pres_arr})

    # ALR-class platforms genuinely have thrusters, so their long flat non-surface
    # stretches are propelled (6); everything else can only be drifting there, so
    # it's parking (4). No ALR marker variable exists in OG1 files, so - as
    # before the pelagos_py port - this is detected from the filename/path.
    is_alr = "ALR" in str(filepath).upper()
    transect_phase = _PROPELLED if is_alr else _PARKING
    mapped_df = _classify_profiles(df_raw, "PRES", transect_phase)

    arrays, meta = {}, {}
    mapped_mask = df_raw[["TIME", "PRES"]].notna().all(axis=1).to_numpy()
    out = {
        "SCI_PHASE": mapped_df["SCI_PHASE"].to_numpy(),
        "PROFILE_NUMBER": mapped_df["PROFILE_NUMBER"].to_numpy(),
        "PROFILE_DIRECTION": mapped_df["PROFILE_DIRECTION"].to_numpy(),
        "CYCLE": mapped_df["CYCLE"].to_numpy(),
        "PROFILE_GRADIENT": mapped_df["GRADIENT"].to_numpy(),
    }
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

    One BBP product is derived per raw beta channel (e.g. BBP700 from
    BETA_BACKSCATTERING700, BBP532 from ...532) so multi-wavelength sensors are
    fully represented; the frontend defaults the backscatter plot to BBP700 when
    present (see ``preset_bbp``).
    """
    # Skip if any backscatter product already exists (don't override real data).
    if any(n.upper().startswith("BBP") for n in existing):
        return [], {}, {}

    # Find every raw beta variable (e.g. BETA_BACKSCATTERING532/700). Each distinct
    # wavelength becomes its own BBP product.
    beta_vars = [n for n in names
                 if "BETA" in n.upper() and "BACKSCATTER" in n.upper()
                 and not n.upper().endswith("_QC")]
    if not beta_vars:
        return [], {}, {}

    data = plot_logic._read_vars_cached(filepath, tuple(sorted(beta_vars)))
    if not data:
        return [], {}, {}

    CHI = 1.076                         # chi factor for a ~124° backscatter sensor

    wanted, arrays, meta = [], {}, {}
    for beta_var in beta_vars:
        beta = np.asarray(data.get(beta_var), dtype=float) if beta_var in data else None
        if beta is None or beta.size == 0 or not np.any(np.isfinite(beta)):
            continue

        # Name the output after the sensor wavelength when present (else BBP700).
        m = re.search(r"(\d{3,4})", beta_var)
        base = f"BBP{m.group(1)}" if m else "BBP700"
        # Two beta vars sharing a wavelength (or clashing with a native BBP) —
        # keep the first and skip the rest rather than clobbering.
        if base in arrays or base in existing:
            continue

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
        tw, ta, tm = _compute_time_qc(filepath, log, names, existing, time_var)
        all_wanted.extend(tw)
        all_arrays.update(ta)
        all_meta.update(tm)
    except Exception as e:
        log(f"TIME QC derivation failed ({e})")

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