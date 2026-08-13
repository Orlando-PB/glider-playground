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
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import cache_logic
from . import cycle_profile_logic
from . import live_logic
from . import overlay_logic
from . import plot_logic
from . import spatial_logic
from . import update_logic
from . import waypoint_logic

app = FastAPI()

# Compress responses over ~1 KB. The overlay/currents JSON (coordinate grids)
# compresses ~5x, which is the biggest win for the user on home-internet uplink
# — see the overlay size audit. minimum_size skips tiny payloads where the
# gzip overhead isn't worth it. Negligible CPU cost on the Pi.
# compresslevel=1 (was 5): on a ~10MB plot_data payload, level 5 spends ~157ms
# compressing to 2.13MB while level 1 spends ~36ms to 2.44MB. Trading +0.3MB of
# transfer for ~120ms less server CPU is a clear win on the plot hot path (and the
# big vendor bundles are cached immutably, so their compression only matters once).
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)


# Starlette's default handler for an unhandled exception is a plain-text
# "Internal Server Error" body, not JSON. Every frontend call does
# `await response.json()` unconditionally, so that plain-text body blows up as
# "Unexpected token 'I', "Internal S"... is not valid JSON" and aborts whatever
# chain of awaits was mid-flight (e.g. loadVariables during a dataset swap).
# Returning JSON here means a backend bug degrades to a normal fetch-error the
# frontend can catch, instead of a JSON.parse crash.
@app.exception_handler(Exception)
async def _json_500(request: Request, exc: Exception):
    logging.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# Warm the heavy copernicusmarine import in the background at startup, so the
# first overlay request doesn't pay its ~2s cold-import cost inline (that import
# happens before the per-phase timers, so it otherwise shows up as unattributed
# "other" time on the very first overlay). Daemon thread; failures are harmless.
import threading as _threading
_threading.Thread(target=overlay_logic.warm_up, name="cm-warmup", daemon=True).start()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- SEO (server deployment only) ----------
# These tags are injected into index.html and the robots/sitemap routes are only
# meaningful for the public deployment at glider-playground.co.uk. Local (pip)
# installs run on 127.0.0.1, so injecting canonical/OG/sitemap there would be
# noise — IS_SERVER gates all of it (see cli.py / the publish workflow).
SITE_URL = "https://glider-playground.co.uk"
SEO_TITLE = "Glider Playground — OG1 Glider Data Viewer | National Oceanography Centre"
SEO_DESCRIPTION = (
    "A free tool for exploring OG1 glider data, from the National Oceanography "
    "Centre. View, plot and map ocean glider profiles and trajectories in your "
    "browser."
)

# Built once on first request, then served from memory. The OG image is the
# dashboard screenshot (1600x847, ~1.9:1 — the size link previews want).
_SEO_HEAD = f"""\
    <meta name="description" content="{SEO_DESCRIPTION}">
    <meta name="keywords" content="OG1, glider data viewer, ocean glider, OG1 data viewer, National Oceanography Centre, NOC OG1, glider playground, ocean data tool, oceanography">
    <meta name="author" content="National Oceanography Centre">
    <meta name="robots" content="index, follow">
    <link rel="canonical" href="{SITE_URL}/">
    <!-- Open Graph (link previews on Slack, Teams, Discord, Facebook, etc.) -->
    <meta property="og:type" content="website">
    <meta property="og:site_name" content="Glider Playground">
    <meta property="og:title" content="{SEO_TITLE}">
    <meta property="og:description" content="{SEO_DESCRIPTION}">
    <meta property="og:url" content="{SITE_URL}/">
    <meta property="og:image" content="{SITE_URL}/static/dashboard.webp">
    <meta property="og:image:type" content="image/webp">
    <meta property="og:image:width" content="1600">
    <meta property="og:image:height" content="847">
    <meta property="og:image:alt" content="The Glider Playground dashboard showing OG1 glider data plots and a map">
    <meta property="og:locale" content="en_GB">
    <!-- Twitter / X large-image card -->
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="{SEO_TITLE}">
    <meta name="twitter:description" content="{SEO_DESCRIPTION}">
    <meta name="twitter:image" content="{SITE_URL}/static/dashboard.webp">
    <meta name="twitter:image:alt" content="The Glider Playground dashboard showing OG1 glider data plots and a map">
    <!-- Structured data: helps search engines understand this is a web app/tool -->
    <script type="application/ld+json">
    {{
      "@context": "https://schema.org",
      "@type": "WebApplication",
      "name": "Glider Playground",
      "alternateName": "OG1 Glider Data Viewer",
      "url": "{SITE_URL}/",
      "description": "{SEO_DESCRIPTION}",
      "applicationCategory": "ScientificApplication",
      "operatingSystem": "Any",
      "browserRequirements": "Requires JavaScript",
      "image": "{SITE_URL}/static/dashboard.webp",
      "isAccessibleForFree": true,
      "offers": {{"@type": "Offer", "price": "0", "priceCurrency": "GBP"}},
      "creator": {{
        "@type": "Organization",
        "name": "National Oceanography Centre",
        "url": "https://www.noc.ac.uk/"
      }}
    }}
    </script>
"""

_seo_html_cache: str | None = None

# HTML snippets contributed by server-only plugins (see _load_server_plugins),
# injected into the served index.html. Empty on every non-server / non-plugin
# install, so this is a no-op for pip users.
_PLUGIN_BODY: list[str] = []


def _is_server() -> bool:
    return os.getenv("IS_SERVER") == "True"


def _index_html() -> str:
    """index.html with SEO tags injected (server mode) — cached after first build."""
    global _seo_html_cache
    if _seo_html_cache is None:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        # Use an SEO-rich <title> for search results / link previews; the in-app
        # UI doesn't rely on the tab title, so this is safe to override.
        html = html.replace(
            "<title>Glider Playground</title>",
            f"<title>{SEO_TITLE}</title>\n{_SEO_HEAD.rstrip()}",
            1,
        )
        # Let server plugins (e.g. the private analytics beacon) inject markup
        # right before </body>.
        if _PLUGIN_BODY:
            html = html.replace("</body>", "\n".join(_PLUGIN_BODY) + "\n</body>", 1)
        _seo_html_cache = html
    return _seo_html_cache


# Vendor bundles are immutable (version is baked into the filename, e.g.
# plotly-gl2d-2.32.0.min.js) so they can be cached forever — important since the
# plot iframe reloads them on every re-plot. Our own source (HTML + the small
# helper scripts/styles) changes between releases, so it must revalidate every
# load or users run stale code after an auto-update (e.g. a cached console_log.js
# missing a newly-added helper).
_IMMUTABLE_SUFFIXES = (".min.js", ".woff", ".woff2", ".ttf", ".png", ".webp",
                       ".svg", ".jpg", ".jpeg", ".gif", ".ico", ".icns")


@app.middleware("http")
async def log_request_timing(request, call_next):
    t0 = time.time()
    response = await call_next(request)
    response.headers["X-Process-Time"] = str(time.time() - t0)

    path = request.url.path
    if path == "/" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache"
    elif path.startswith("/static/"):
        if path.endswith(_IMMUTABLE_SUFFIXES):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # console_log.js, cycle_profile.js, tailwind.css — revalidate (cheap 304).
            response.headers["Cache-Control"] = "no-cache"
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
    # Inject SEO tags only for the public deployment; local installs get
    # the unmodified file straight from disk.
    if _is_server():
        return Response(content=_index_html(), media_type="text/html")
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/robots.txt")
def robots_txt():
    if _is_server():
        body = (
            "User-agent: *\n"
            "Allow: /\n"
            "# API and per-file data endpoints aren't useful to index.\n"
            "Disallow: /api/\n"
            f"\nSitemap: {SITE_URL}/sitemap.xml\n"
        )
    else:
        # Local install on 127.0.0.1 — nothing to crawl.
        body = "User-agent: *\nDisallow: /\n"
    return Response(content=body, media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    if not _is_server():
        raise HTTPException(status_code=404, detail="Not found")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url>\n    <loc>{SITE_URL}/</loc>\n"
        "    <changefreq>weekly</changefreq>\n    <priority>1.0</priority>\n  </url>\n"
        "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


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


@app.get("/api/kmz")
def api_kmz(id: str):
    """Download the glider's surface track as a KMZ for Google Earth."""
    path = _resolve_path(id)
    rec = cache_logic.get_record(id)
    name = (rec.get("name") if rec else None) or os.path.basename(path)
    stem = os.path.splitext(name)[0]
    try:
        kmz = spatial_logic.generate_kmz(path, stem)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not build KMZ: {e}")
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in stem) or "glider_track"
    return Response(
        content=kmz,
        media_type="application/vnd.google-earth.kmz",
        headers={"Content-Disposition": f'attachment; filename="{safe}.kmz"'},
    )


def _downsample_path(path: list, cap: int) -> list:
    """Evenly thin a [[lat,lon],...] track to at most `cap` points, always
    keeping the first and last fix. The globe further downsamples to its own
    segment cap, so this just bounds the JSON transfer for the all-tracks view.
    """
    n = len(path)
    if n <= cap:
        return path
    stride = -(-(n - 1) // cap)  # ceil
    out = [path[i] for i in range(0, n, stride)]
    if out[-1] is not path[-1]:
        out.append(path[-1])
    return out


@app.get("/api/map_all")
def api_map_all():
    """Lightweight paths for every processed (ready) file — the globe draws
    these as grey context tracks behind the active (yellow) one. Active-file
    detail (surface overlays / currents) still comes from the per-id endpoints;
    everything here is read straight from the already-cached `map` payloads, so
    there's no extra processing.
    """
    tracks = []
    for rec in cache_logic.list_files():
        if rec.get("status") != "ready":
            continue
        fid = rec["id"]
        payload = cache_logic.get_payload(fid, "map")
        if not isinstance(payload, dict) or "error" in payload:
            continue
        path = payload.get("path") or []
        if not path:
            continue
        tracks.append({
            "id": fid,
            "name": rec.get("name"),
            "path": _downsample_path(path, 500 if plot_logic._LOW_MEMORY else 800),
            "dac": payload.get("dac") or [],
            "last_lat": rec.get("last_lat"),
            "last_lon": rec.get("last_lon"),
            "is_nrt": rec.get("is_nrt"),
        })
    return {"tracks": tracks}


@app.get("/api/waypoints")
def api_waypoints(glider: str | None = None):
    """Manually curated target points (e.g. planned stations) for a glider,
    optionally filtered by a case-insensitive substring match on the `glider`
    tag. Read-only here — managed from the admin panel on the server
    deployment (see deploy/waypoints_admin.py)."""
    return {"waypoints": waypoint_logic.list_waypoints(glider)}


@app.get("/api/3d_data")
def api_3d_data(id: str):
    return _cached_or_live(id, "spatial_3d", spatial_logic.generate_3d_data)


@app.get("/api/location")
def api_location(id: str):
    return _cached_or_live(id, "location", spatial_logic.get_location_summary)


@app.get("/api/nearest_fix")
def api_nearest_fix(id: str, time: float):
    """Closest GPS fix (lat/lon) to a given epoch-ms ``time`` — used to pin a
    clicked plot point onto the globe. Not cached: the time varies per click."""
    return spatial_logic.get_nearest_fix(_resolve_path(id), time)


@app.get("/api/nearest_fix_by_coord")
def api_nearest_fix_by_coord(id: str, lat: float, lon: float):
    """Closest GPS fix to a clicked ``lat``/``lon`` — the inverse of
    ``/api/nearest_fix``. Resolves a globe click on the glider's path to the TIME
    there, so the matching point can be marked on every open plot. Not cached:
    the position varies per click."""
    return spatial_logic.get_nearest_fix_by_coord(_resolve_path(id), lat, lon)


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
    response: Response,
    id: str, x_var: str, y_var: str, c_var: str = "",
    apply_qc: bool = False, qc_flags: str = "1,2,5,8", highlight_qc: bool = False, filter_time: bool = True,
    profile_num: float = None,
    cycle_num: float = None, cycle_var: str = None, sci_phases: str = "", direction_filter: str = "",
    ctd_interpolate: bool = False, ctd_qc: bool = False, highlight_profile: bool = False,
    max_points: int = None,
    zoom_x_var: str = None,
    zoom_x_min: float = None, zoom_x_max: float = None,
    zoom_y_min: float = None, zoom_y_max: float = None,
    binary: bool = False,
) -> dict:
    phases = [int(p) for p in sci_phases.split(",") if p.strip().lstrip("-").isdigit()] if sci_phases else None
    dirs = [int(d) for d in direction_filter.split(",") if d.strip().lstrip("-").isdigit()] if direction_filter else None

    # Cache the packed binary payload per (file signature + version + params): a hit
    # skips the entire read→filter→downsample→pack pipeline. Only the binary form is
    # cached (it's what we send); the JSON path is the rare error/fallback case.
    cache_key = plot_logic.plot_cache_params_str(
        x_var=x_var, y_var=y_var, c_var=c_var, apply_qc=apply_qc, qc_flags=qc_flags,
        highlight_qc=highlight_qc, filter_time=filter_time, profile_num=profile_num,
        cycle_num=cycle_num, cycle_var=cycle_var, sci_phases=sci_phases,
        direction_filter=direction_filter, ctd_interpolate=ctd_interpolate, ctd_qc=ctd_qc,
        highlight_profile=highlight_profile, max_points=max_points,
        zoom_x_var=zoom_x_var, zoom_x_min=zoom_x_min, zoom_x_max=zoom_x_max,
        zoom_y_min=zoom_y_min, zoom_y_max=zoom_y_max,
    ) if binary else None
    if cache_key is not None:
        hit = cache_logic.get_plot_binary(id, cache_key)
        if hit is not None:
            return Response(content=hit, media_type="application/octet-stream",
                            headers={"Server-Timing": "cache;dur=0"})

    # Per-step server timings, surfaced to the frontend's PLOT log as a Server-Timing
    # header so the "server" phase can be broken down (read / filter / serialize / ...).
    timings = {}
    # binary=True returns a packed octet-stream (uint32 header len + JSON header +
    # raw LE typed arrays) so the browser skips JSON.parse of ~500k numbers and the
    # server skips the astype(object)/tolist + JSON encode. The `-> dict` annotation
    # still drives the JSON path (pydantic fast-paths plain lists straight to bytes);
    # returning a Response short-circuits that and is passed through untouched.
    result = plot_logic.get_plot_data_json(
        _resolve_path(id), x_var, y_var, c_var,
        apply_qc=apply_qc, qc_flags=qc_flags, highlight_qc=highlight_qc,
        filter_time=filter_time, profile_num=profile_num,
        cycle_num=cycle_num, cycle_var=cycle_var, sci_phases=phases, direction_filter=dirs,
        ctd_interpolate=ctd_interpolate, ctd_qc=ctd_qc, highlight_profile=highlight_profile,
        max_points=max_points,
        zoom_x_var=zoom_x_var, zoom_x_min=zoom_x_min, zoom_x_max=zoom_x_max,
        zoom_y_min=zoom_y_min, zoom_y_max=zoom_y_max,
        timings=timings, binary=binary,
    )
    # e.g. "read;dur=120.5, filter;dur=8.2, serialize;dur=45.0"
    server_timing = ", ".join(f"{k};dur={v:.1f}" for k, v in timings.items()) if timings else None
    # A packed payload comes back as bytes; an error (or the JSON path) as a dict.
    if isinstance(result, (bytes, bytearray)):
        if cache_key is not None:
            cache_logic.put_plot_binary(id, cache_key, bytes(result))
        headers = {"Server-Timing": server_timing} if server_timing else None
        return Response(content=bytes(result), media_type="application/octet-stream", headers=headers)
    if server_timing:
        response.headers["Server-Timing"] = server_timing
    return result


@app.get("/api/plot_data_bounds")
def api_plot_data_bounds(
    id: str, x_var: str, y_var: str, c_var: str = "",
    apply_qc: bool = False, qc_flags: str = "1,2,5,8", highlight_qc: bool = False, filter_time: bool = True,
    x_min: float = None, x_max: float = None, y_min: float = None, y_max: float = None,
    view_x_min: float = None, view_x_max: float = None, view_y_min: float = None, view_y_max: float = None,
    profile_num: float = None,
    cycle_num: float = None, cycle_var: str = None, sci_phases: str = "", direction_filter: str = "",
    ctd_interpolate: bool = False, ctd_qc: bool = False, highlight_profile: bool = False,
    max_points: int = None,
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
        max_points=max_points,
    )


# ---------- satellite / model overlays ----------

@app.get("/api/overlays")
def api_overlays():
    """List of overlay variables the map view can request."""
    return {"overlays": list(overlay_logic.OVERLAYS.keys())}


@app.get("/api/copernicus/status")
def api_copernicus_status():
    """Whether Copernicus Marine credentials are set up on this machine."""
    return {"logged_in": overlay_logic.credentials_present()}


@app.post("/api/copernicus/login")
async def api_copernicus_login(request: Request):
    """Validate + persist Copernicus Marine credentials entered in the app, so
    overlays work without running 'copernicusmarine login' in a terminal."""
    if os.getenv("IS_SERVER") == "True":
        raise HTTPException(status_code=403, detail="Copernicus login not available in server mode")
    body = await request.json()
    return overlay_logic.login(body.get("username"), body.get("password"))


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

    t_loc = time.time()
    loc = _cached_or_live(id, "location", spatial_logic.get_location_summary)
    if not loc or "error" in loc:
        raise HTTPException(status_code=404, detail="No spatial data for this file")

    rec = cache_logic.get_record(id)
    target_date = _overlay_target_date(rec)
    locate = time.time() - t_loc

    result = overlay_logic.fetch_overlay(
        var,
        lat_min=loc["lat_min"],
        lat_max=loc["lat_max"],
        lon_min=loc["lon_min"],
        lon_max=loc["lon_max"],
        target_date=target_date,
    )
    # An error comes back as a plain dict → JSON (the rare fallback path).
    if "error" in result:
        return result
    # Resolving the glider's bbox/date is part of this request's wall time, so
    # report it alongside the fetch's own phases for the unified client log.
    if isinstance(result.get("_timing"), dict):
        result["_timing"]["locate"] = locate
    # Ship the cell grid as a packed binary payload (uint32 header len + JSON
    # header + raw LE float32 lat/lon/val) so the browser skips JSON.parse of a
    # ~100k-element list and the server skips the JSON text encode.
    return Response(
        content=overlay_logic.pack_overlay_response(result),
        media_type="application/octet-stream",
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


# ---------- server-only plugins ----------

def _load_server_plugins() -> None:
    """Load optional, private server-only extensions.

    These live *outside* this package (and outside the public repo / PyPI
    release) so the deployment can add things like usage analytics without that
    code shipping to local/pip users. Each .py file in the plugins dir
    may define ``register(app)`` to add routes and/or a ``BEACON_HTML`` string
    injected into index.html. Loaded only in server mode; absence is the normal
    case (so pip installs do nothing here and pay no overhead).

    Plugins dir: $GP_PLUGINS_DIR, else ~/.glider_playground/plugins.
    """
    if not _is_server():
        return
    import glob
    import importlib.util

    plugin_dir = os.getenv("GP_PLUGINS_DIR") or os.path.expanduser(
        "~/.glider_playground/plugins"
    )
    for path in sorted(glob.glob(os.path.join(plugin_dir, "*.py"))):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            spec = importlib.util.spec_from_file_location(f"gp_plugin_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "register"):
                mod.register(app)
            snippet = getattr(mod, "BEACON_HTML", "")
            if snippet:
                _PLUGIN_BODY.append(snippet)
            logger.info("Loaded server plugin: %s", name)
        except Exception:
            logger.exception("Failed to load server plugin %s", path)


_load_server_plugins()


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8420, reload=True, access_log=False)
