"""FastAPI app — file management and cached data endpoints."""

import logging
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import cache_logic
from . import cycle_profile_logic
from . import live_logic
from . import overlay_logic
from . import plot_logic
from . import spatial_logic
from . import update_logic

app = FastAPI()

# Compress responses over ~1 KB. The overlay/currents JSON (coordinate grids)
# compresses ~5x, which is the biggest win for the user on home-internet uplink
# — see the overlay size audit. minimum_size skips tiny payloads where the
# gzip overhead isn't worth it. Negligible CPU cost on the Pi.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def log_request_timing(request, call_next):
    t0 = time.time()
    response = await call_next(request)
    response.headers["X-Process-Time"] = str(time.time() - t0)
    return response


# ---------- helpers ----------

def _resolve_path(file_id: str) -> str:
    path = cache_logic.resolve_path(file_id)
    if not path:
        raise HTTPException(status_code=404, detail="Unknown file id")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File no longer exists")
    return path


def _cached_or_live(file_id: str, key: str, compute):
    """Return the precomputed payload if ready; otherwise compute live.
    The live fallback is the safety net for clicking before processing finishes.
    """
    cached = cache_logic.get_payload(file_id, key)
    if cached is not None:
        return cached
    return compute(_resolve_path(file_id))


# ---------- root / config ----------

@app.get("/")
def read_root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/config")
def get_config():
    try:
        import importlib.metadata
        version = importlib.metadata.version("glider-playground")
    except Exception:
        version = "unknown"
    is_server = os.getenv("IS_SERVER") == "True"
    return {
        "is_server": is_server,
        "version": version,
        "throttle": is_server,
        "low_memory": os.getenv("LOW_MEMORY_MODE", "").lower() in ("1", "true", "yes"),
    }


# ---------- file management ----------

@app.get("/api/files")
def get_files():
    return {"files": cache_logic.list_files()}


@app.get("/api/files/{file_id}")
def get_file(file_id: str):
    rec = cache_logic.get_record(file_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown file id")
    return cache_logic._public_view(rec)


@app.delete("/api/files/{file_id}")
def delete_file(file_id: str):
    if os.getenv("IS_SERVER") == "True":
        raise HTTPException(status_code=403, detail="File deletion not available in server mode")
    if not cache_logic.remove_file(file_id):
        raise HTTPException(status_code=404, detail="Unknown file id")
    return {"status": "success"}


@app.post("/api/files/refresh/{file_id}")
def refresh_file(file_id: str):
    view = cache_logic.request_refresh(file_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown file id")
    return view


@app.post("/api/files/register")
async def register_file(request: Request):
    """Register an existing file by absolute path (local mode)."""
    body = await request.json()
    path = (body.get("path") or "").strip()
    if not path:
        return {"status": "error", "message": "No path provided"}
    try:
        return {"status": "success", "file": cache_logic.register_path(path)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/files/upload")
async def upload_files(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        try:
            content = await f.read()
            results.append({"status": "success", "file": cache_logic.save_upload(f.filename or "uploaded.nc", content)})
        except Exception as e:
            results.append({"status": "error", "filename": f.filename, "message": str(e)})
    return {"results": results}


def _native_picker(args_darwin, args_other) -> str:
    """Run a native picker subprocess; return its stdout (one path per line)."""
    cmd = args_darwin if platform.system() == "Darwin" else args_other
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def _register_paths(paths):
    out = []
    for p in paths:
        try:
            out.append({"status": "success", "file": cache_logic.register_path(p)})
        except Exception as e:
            out.append({"status": "error", "path": p, "message": str(e)})
    return out


@app.post("/api/files/pick")
def pick_files():
    """Native multi-file picker (local only)."""
    if os.getenv("IS_SERVER") == "True":
        return {"status": "error", "message": "File picker not available in server mode"}

    darwin = [
        "osascript", "-e",
        'set fs to choose file with prompt "Select NetCDF files" of type {"nc"} with multiple selections allowed',
        "-e", 'set p to ""',
        "-e", 'repeat with f in fs',
        "-e", '  set p to p & POSIX path of f & "\n"',
        "-e", 'end repeat',
        "-e", 'return p',
    ]
    other = [
        sys.executable, "-c",
        "import tkinter as tk; from tkinter import filedialog; "
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); "
        "files = filedialog.askopenfilenames(filetypes=[('NetCDF','*.nc')]); "
        "print('\\n'.join(files))"
    ]
    try:
        paths = [p for p in _native_picker(darwin, other).splitlines() if p]
    except Exception as e:
        return {"status": "error", "message": str(e)}

    if not paths:
        return {"status": "cancelled"}
    return {"status": "success", "results": _register_paths(paths)}


@app.post("/api/files/pick_folder")
def pick_folder():
    """Native folder picker; registers every .nc inside (recursively)."""
    if os.getenv("IS_SERVER") == "True":
        return {"status": "error", "message": "Folder picker not available in server mode"}

    darwin = [
        "osascript", "-e",
        "tell application (path to frontmost application as text) to return POSIX path of (choose folder)"
    ]
    other = [
        sys.executable, "-c",
        "import tkinter as tk; from tkinter import filedialog; "
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); "
        "print(filedialog.askdirectory())"
    ]
    try:
        folder = _native_picker(darwin, other)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    if not folder or not os.path.isdir(folder):
        return {"status": "cancelled"}

    paths = [str(p) for p in sorted(Path(folder).rglob("*.nc"))]
    if not paths:
        return {"status": "empty", "path": folder}
    return {"status": "success", "path": folder, "results": _register_paths(paths)}


@app.get("/api/update_check")
def api_update_check(force: bool = False):
    """Is a newer release on PyPI, and how should this install upgrade?"""
    return update_logic.check(force=force)


@app.get("/api/live")
def api_live(force: bool = False):
    """Active gliders + uploads. Server-side cache prevents Pi flooding."""
    return live_logic.list_live(force_scan=force)


@app.post("/api/live/download")
def api_live_download(filename: str):
    return live_logic.request_download(filename)


@app.delete("/api/live/{filename}")
def api_live_delete(filename: str):
    if not live_logic.delete_managed(filename):
        raise HTTPException(status_code=404, detail="Not a managed file")
    return {"status": "ok"}


# ---------- per-file data endpoints ----------

@app.get("/api/map")
def api_map(id: str):
    payload = _cached_or_live(id, "map", spatial_logic.generate_map_image)
    # Backfill DAC for map payloads cached before DAC support was added — the
    # extraction is itself cached, so this is cheap on the warm path.
    if isinstance(payload, dict) and "error" not in payload and "dac" not in payload:
        try:
            payload = {**payload, "dac": spatial_logic.get_dac_vectors(_resolve_path(id))}
        except Exception:
            payload = {**payload, "dac": []}
    # Decorate with NRT info so the map view can render a live-position marker.
    rec = cache_logic.get_record(id)
    if isinstance(payload, dict) and rec:
        payload = {
            **payload,
            "last_lat": rec.get("last_lat"),
            "last_lon": rec.get("last_lon"),
            "last_time": rec.get("last_time"),
            "is_nrt": cache_logic._is_nrt(rec.get("last_time")),
        }
    return payload


@app.get("/api/3d_data")
def api_3d_data(id: str):
    return _cached_or_live(id, "spatial_3d", spatial_logic.generate_3d_data)


@app.get("/api/location")
def api_location(id: str):
    return _cached_or_live(id, "location", spatial_logic.get_location_summary)


@app.get("/api/variables")
def api_variables(id: str):
    cached = cache_logic.get_payload(id, "variables")
    if cached is not None:
        return {"variables": cached}
    return {"variables": plot_logic.get_variables(_resolve_path(id))}


@app.get("/api/dataset_info")
def api_dataset_info(id: str):
    return _cached_or_live(id, "dataset_info", plot_logic.get_dataset_info)


@app.get("/api/profiles")
def api_profiles(id: str):
    return _cached_or_live(id, "profiles", plot_logic.get_profiles)


@app.get("/api/cycles")
def api_cycles(id: str):
    return cycle_profile_logic.get_cycles(_resolve_path(id))


@app.get("/api/plot_data")
def api_plot_data(
    id: str, x_var: str, y_var: str, c_var: str = "",
    apply_qc: bool = False, qc_flags: str = "1,2,5,8", highlight_qc: bool = False, filter_time: bool = True,
    profile_num: float = None,
    cycle_num: float = None, cycle_var: str = None, sci_phases: str = "", direction_filter: str = "",
    ctd_interpolate: bool = False, ctd_qc: bool = False, highlight_profile: bool = False,
    max_points: int = None,
) -> dict:
    phases = [int(p) for p in sci_phases.split(",") if p.strip().lstrip("-").isdigit()] if sci_phases else None
    dirs = [int(d) for d in direction_filter.split(",") if d.strip().lstrip("-").isdigit()] if direction_filter else None
    # The `-> dict` annotation makes FastAPI serialize straight to JSON bytes via
    # pydantic, skipping the jsonable_encoder pass that dominates on big arrays.
    # plot_logic returns plain lists (NaN already -> None) so this stays valid JSON.
    return plot_logic.get_plot_data_json(
        _resolve_path(id), x_var, y_var, c_var,
        apply_qc=apply_qc, qc_flags=qc_flags, highlight_qc=highlight_qc,
        filter_time=filter_time, profile_num=profile_num,
        cycle_num=cycle_num, cycle_var=cycle_var, sci_phases=phases, direction_filter=dirs,
        ctd_interpolate=ctd_interpolate, ctd_qc=ctd_qc, highlight_profile=highlight_profile,
        max_points=max_points,
    )


@app.get("/api/plot_data_bounds")
def api_plot_data_bounds(
    id: str, x_var: str, y_var: str, c_var: str = "",
    apply_qc: bool = False, qc_flags: str = "1,2,5,8", highlight_qc: bool = False, filter_time: bool = True,
    x_min: float = None, x_max: float = None, y_min: float = None, y_max: float = None,
    view_x_min: float = None, view_x_max: float = None, view_y_min: float = None, view_y_max: float = None,
    profile_num: float = None,
    cycle_num: float = None, cycle_var: str = None, sci_phases: str = "", direction_filter: str = "",
    ctd_interpolate: bool = False, ctd_qc: bool = False, highlight_profile: bool = False,
) -> dict:
    phases = [int(p) for p in sci_phases.split(",") if p.strip().lstrip("-").isdigit()] if sci_phases else None
    dirs = [int(d) for d in direction_filter.split(",") if d.strip().lstrip("-").isdigit()] if direction_filter else None
    return plot_logic.get_plot_data_bounds(
        _resolve_path(id), x_var, y_var, c_var,
        apply_qc=apply_qc, qc_flags=qc_flags, highlight_qc=highlight_qc,
        filter_time=filter_time,
        x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max,
        view_x_min=view_x_min, view_x_max=view_x_max, view_y_min=view_y_min, view_y_max=view_y_max,
        profile_num=profile_num,
        cycle_num=cycle_num, cycle_var=cycle_var, sci_phases=phases, direction_filter=dirs,
        ctd_interpolate=ctd_interpolate, ctd_qc=ctd_qc, highlight_profile=highlight_profile,
    )


# ---------- satellite / model overlays ----------

@app.get("/api/overlays")
def api_overlays():
    """List of overlay variables the map view can request."""
    return {"overlays": list(overlay_logic.OVERLAYS.keys())}


# A glider whose last fix is within this many days is treated as "live": its
# overlay uses the most recent available Copernicus field rather than the exact
# last-fix date, so an active deployment always sees the freshest ocean state.
_LIVE_WINDOW_DAYS = 7


def _overlay_target_date(rec) -> str | None:
    """Pick the overlay date for a file: the glider's last data point for a past
    deployment, or None (→ most recent available) when the glider is still live.

    overlay_logic caps a future/last date to the dataset's latest day anyway, so
    this mainly matters for a glider whose last fix is a few days old but still
    within the live window — we want the latest field, not that slightly-stale day.
    """
    if not rec or not rec.get("last_time"):
        return None
    last_str = str(rec["last_time"])[:10]
    try:
        last = datetime.strptime(last_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return last_str  # unparseable — fall back to the contemporaneous date
    age_days = (datetime.now(timezone.utc) - last).days
    return None if age_days <= _LIVE_WINDOW_DAYS else last_str


@app.get("/api/overlay")
def api_overlay(id: str, var: str):
    """Surface overlay (chla/temp/salinity/o2/ph/biomass/sla) for a file's bbox.

    For a past deployment the date is tied to the glider's last GPS fix so the
    field is contemporaneous with the track; for a still-live glider (last fix
    within the live window) it uses the most recent available field instead. See
    _overlay_target_date.
    """
    if var not in overlay_logic.OVERLAYS:
        raise HTTPException(status_code=404, detail=f"Unknown overlay '{var}'")

    loc = _cached_or_live(id, "location", spatial_logic.get_location_summary)
    if not loc or "error" in loc:
        raise HTTPException(status_code=404, detail="No spatial data for this file")

    rec = cache_logic.get_record(id)
    target_date = _overlay_target_date(rec)

    return overlay_logic.fetch_overlay(
        var,
        lat_min=loc["lat_min"],
        lat_max=loc["lat_max"],
        lon_min=loc["lon_min"],
        lon_max=loc["lon_max"],
        target_date=target_date,
    )


@app.get("/api/currents")
def api_currents(id: str):
    """Surface current (uo/vo) grid for a file's bbox, for the animated flow layer.

    Like /api/overlay, the date follows the glider's last fix for a past
    deployment and the most recent available field for a still-live glider.
    """
    loc = _cached_or_live(id, "location", spatial_logic.get_location_summary)
    if not loc or "error" in loc:
        raise HTTPException(status_code=404, detail="No spatial data for this file")

    rec = cache_logic.get_record(id)
    target_date = _overlay_target_date(rec)

    return overlay_logic.fetch_currents(
        lat_min=loc["lat_min"],
        lat_max=loc["lat_max"],
        lon_min=loc["lon_min"],
        lon_max=loc["lon_max"],
        target_date=target_date,
    )


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8420, reload=True, access_log=False)
