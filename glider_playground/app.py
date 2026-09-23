"""FastAPI app — every HTTP route, kept thin.

Serves the static frontend and /api/*: files, live ERDDAP deployments, map /
3D / KMZ, plot data, variables / profiles / cycles, Copernicus overlays, and
the extras (Argo, ships, waypoints, update check, plot_presets.js). Handlers
resolve a file id to a path, serve from cache_logic when the payload is
ready, and otherwise call into core/, maps/ or server/.

In server mode only, also loads private plugins from
~/.glider_playground/plugins.
"""

import json
import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .maps import argo_logic
from .core import bathy_prefetch
from .core import cache_logic
from .core import cycle_profile_logic
from .server import erddap_fetch
from .maps import copernicus_fetch
from .maps import copernicus_prefetch
from .core import plot_logic
from .core import presets_logic
from .core import spatial_logic
from .server import update_logic
from .maps import waypoint_logic
from .server import server_config
from .maps import ships_logic
from . import missions

server_config.configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI()

# Gzip responses over ~1 KB (overlay grids compress ~5x). Level 1: on a 10 MB plot payload it costs
# ~36 ms vs ~157 ms at level 5 for only ~0.3 MB more.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)


# Unhandled errors must return JSON: every frontend call does `await response.json()`, and
# Starlette's plain-text 500 would crash that parse instead of surfacing as a normal fetch error.
@app.exception_handler(Exception)
async def _json_500(request: Request, exc: Exception):
    logging.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# Warm the heavy copernicusmarine import in the background at startup, so the
# first overlay request doesn't pay its ~2s cold-import cost inline (that import
# happens before the per-phase timers, so it otherwise shows up as unattributed
# "other" time on the very first overlay). Daemon thread; failures are harmless.
import threading as _threading
_threading.Thread(target=copernicus_fetch.warm_up, name="cm-warmup", daemon=True).start()
# Background overlay prefetch: every READY file gets its Copernicus layers
# fetched once and stored on disk (see copernicus_prefetch).
copernicus_prefetch.start()
bathy_prefetch.start()

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
missions.attach(app)   # experimental, self-contained: see missions/README.md


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
    <meta property="og:image" content="{SITE_URL}/static/readme_images/dashboard.webp">
    <meta property="og:image:type" content="image/webp">
    <meta property="og:image:width" content="1600">
    <meta property="og:image:height" content="847">
    <meta property="og:image:alt" content="The Glider Playground dashboard showing OG1 glider data plots and a map">
    <meta property="og:locale" content="en_GB">
    <!-- Twitter / X large-image card -->
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="{SEO_TITLE}">
    <meta name="twitter:description" content="{SEO_DESCRIPTION}">
    <meta name="twitter:image" content="{SITE_URL}/static/readme_images/dashboard.webp">
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
      "image": "{SITE_URL}/static/readme_images/dashboard.webp",
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
    return server_config.IS_SERVER


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


# Versioned vendor bundles and images are cached forever; our own HTML/JS/CSS must revalidate
# every load so an auto-update never leaves users on stale code.
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
    is_server = server_config.IS_SERVER
    return {
        "is_server": is_server,
        "version": version,
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
    if server_config.IS_SERVER:
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
    if server_config.IS_SERVER:
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
    if server_config.IS_SERVER:
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
    return erddap_fetch.list_live(force_scan=force)


@app.post("/api/live/download")
def api_live_download(filename: str):
    return erddap_fetch.request_download(filename)


@app.delete("/api/live/{filename}")
def api_live_delete(filename: str):
    if not erddap_fetch.delete_managed(filename):
        raise HTTPException(status_code=404, detail="Not a managed file")
    return {"status": "ok"}


# ---------- Argo float explorer (experimental; see argo_logic.py) ----------

@app.get("/api/argo/floats")
def api_argo_floats(days: float = 30):
    """Last known position of every Argo float active within `days` (0 = all)."""
    return argo_logic.list_floats(days=days or None)


@app.get("/api/argo/float/{wmo}")
def api_argo_float(wmo: str):
    return argo_logic.float_detail(wmo)


@app.get("/api/argo/profiles")
def api_argo_profiles(min_lat: float, max_lat: float, min_lon: float, max_lon: float, t0: float, t1: float):
    """Argo profiles (surfacings) inside a lat/lon box between epoch-ms t0 and t1 — for the 3D view."""
    return argo_logic.profiles_in(min_lat, max_lat, min_lon, max_lon, t0, t1)


# ---------- Research ships (experimental; see ships_logic.py) ----------

@app.get("/api/ships")
def api_ships():
    """Latest reported position of RRS Discovery / James Cook / Sir David Attenborough."""
    return ships_logic.list_ships()


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
            "path": _downsample_path(path, 800),
            "dac": payload.get("dac") or [],
            "last_lat": rec.get("last_lat"),
            "last_lon": rec.get("last_lon"),
            "is_nrt": rec.get("is_nrt"),
            "last_time": rec.get("last_time"),
            "platform_kind": rec.get("platform_kind") or "",
        })
    return {"tracks": tracks}


@app.get("/api/waypoints")
def api_waypoints(glider: str | None = None):
    """Manually curated target points (e.g. planned stations) for a glider,
    optionally filtered by a case-insensitive substring match on the `glider`
    tag. Read-only here — managed from the admin panel on the server
    deployment (a server-only plugin)."""
    return {"waypoints": waypoint_logic.list_waypoints(glider)}


@app.get("/api/3d_data")
def api_3d_data(id: str):
    payload = _cached_or_live(id, "spatial_3d", spatial_logic.generate_3d_data)
    # A bathymetry fetch that failed during processing is cached as a flat floor; retry it here.
    if spatial_logic.retry_bathy(payload):
        rec = cache_logic.get_record(id)
        if rec and rec.get("spatial_3d") is payload:
            cache_logic._save_payload_sidecar(rec)
    return payload


@app.get("/api/3d_bathy")
def api_3d_bathy(id: str, grid: int = bathy_prefetch.GRID):
    """Fine seabed for the file's 3D box, for the three.js 3D view: normally already stored by bathy_prefetch."""
    try:
        _cached_or_live(id, "spatial_3d", spatial_logic.generate_3d_data)      # the box comes from the 3D payload
        return Response(content=bathy_prefetch.fetch(id, max(50, min(grid, 1200))), media_type="application/json")
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Seabed unavailable: {e}")


@app.get("/api/3d_track")
def api_3d_track(id: str):
    """The file's 3D track as packed binary (see bathy_prefetch.track), for the three.js 3D view."""
    try:
        _cached_or_live(id, "spatial_3d", spatial_logic.generate_3d_data)
        return Response(content=bathy_prefetch.track(id), media_type="application/octet-stream")
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/3d_colours")
def api_3d_colours(id: str):
    """Presets this file's 3D track can be coloured by (see spatial_logic.track_colour)."""
    return {"options": spatial_logic.track_colour_options(_resolve_path(id))}


@app.get("/api/3d_colour")
def api_3d_colour(id: str, preset: str = "", var: str = "", cmap: str = ""):
    return spatial_logic.track_colour(_resolve_path(id), preset or None, var or None, cmap or None)


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


@app.get("/api/plot_presets.js")
def api_plot_presets_js():
    # glider_playground/plot_presets.json as a blocking script (window.GP_PLOT_PRESETS) so the
    # pages have presets/palettes synchronously — no toolbar reflow after first paint.
    return Response(presets_logic.as_script(), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/variables")
def api_variables(id: str):
    cached = cache_logic.get_payload(id, "variables")
    if cached is not None:
        return {"variables": cached}
    return {"variables": plot_logic.get_variables(_resolve_path(id))}


@app.get("/api/var_plot")
def api_var_plot(id: str, var: str):
    """Stats view: a clicked variable against TIME (see plot_logic.get_var_time_series)."""
    _resolve_path(id)
    out = cache_logic.get_var_preview(id, var)
    if "error" in out:
        raise HTTPException(status_code=400, detail=out["error"])
    return out


@app.get("/api/dataset_info")
def api_dataset_info(id: str):
    info = _cached_or_live(id, "dataset_info", plot_logic.get_dataset_info)
    if isinstance(info, dict) and not info.get("error"):
        # Plottable TIME extent, so the shell can pre-align synced time axes (not part of the cached payload).
        t0, t1 = spatial_logic.get_time_extent_iso(_resolve_path(id))
        info = {**info, "time_min": t0, "time_max": t1}
    return info


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
    qc_flags: str = "0,1,2,5,8",
    profile_num: float = None,
    cycle_num: float = None, cycle_var: str = None, sci_phases: str = "", direction_filter: str = "",
    highlight_profile: bool = False,
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
        x_var=x_var, y_var=y_var, c_var=c_var, qc_flags=qc_flags,
        profile_num=profile_num,
        cycle_num=cycle_num, cycle_var=cycle_var, sci_phases=sci_phases,
        direction_filter=direction_filter,
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
        qc_flags=qc_flags,
        profile_num=profile_num,
        cycle_num=cycle_num, cycle_var=cycle_var, sci_phases=phases, direction_filter=dirs,
        highlight_profile=highlight_profile,
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
    qc_flags: str = "0,1,2,5,8",
    x_min: float = None, x_max: float = None, y_min: float = None, y_max: float = None,
    view_x_min: float = None, view_x_max: float = None, view_y_min: float = None, view_y_max: float = None,
    profile_num: float = None,
    cycle_num: float = None, cycle_var: str = None, sci_phases: str = "", direction_filter: str = "",
    highlight_profile: bool = False,
    max_points: int = None,
    binary: bool = False,
) -> dict:
    phases = [int(p) for p in sci_phases.split(",") if p.strip().lstrip("-").isdigit()] if sci_phases else None
    dirs = [int(d) for d in direction_filter.split(",") if d.strip().lstrip("-").isdigit()] if direction_filter else None
    # binary=True: the same packed container as /api/plot_data (an error still comes back as JSON).
    result = plot_logic.get_plot_data_bounds(
        _resolve_path(id), x_var, y_var, c_var,
        qc_flags=qc_flags,
        x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max,
        view_x_min=view_x_min, view_x_max=view_x_max, view_y_min=view_y_min, view_y_max=view_y_max,
        profile_num=profile_num,
        cycle_num=cycle_num, cycle_var=cycle_var, sci_phases=phases, direction_filter=dirs,
        highlight_profile=highlight_profile,
        max_points=max_points, binary=binary,
    )
    if isinstance(result, (bytes, bytearray)):
        return Response(content=bytes(result), media_type="application/octet-stream")
    return result


# ---------- satellite / model overlays ----------

@app.get("/api/overlays")
def api_overlays():
    """List of overlay variables the map view can request."""
    return {"overlays": list(copernicus_fetch.OVERLAYS.keys())}


@app.get("/api/copernicus/status")
def api_copernicus_status():
    """Whether Copernicus Marine credentials are set up on this machine."""
    return {"logged_in": copernicus_fetch.credentials_present()}


@app.post("/api/copernicus/login")
async def api_copernicus_login(request: Request):
    """Validate + persist Copernicus Marine credentials entered in the app, so
    overlays work without running 'copernicusmarine login' in a terminal."""
    if server_config.IS_SERVER:
        raise HTTPException(status_code=403, detail="Copernicus login not available in server mode")
    body = await request.json()
    out = copernicus_fetch.login(body.get("username"), body.get("password"))
    if out.get("status") == "success":
        copernicus_prefetch.retry_errors()   # layers that failed for lack of creds
    return out


@app.get("/api/overlay_status")
def api_overlay_status(id: str):
    """Per-layer prefetch state for a file (pending/ready/error + field date),
    used by the map view to grey out layers until they're on disk. Asking also
    moves the file to the front of the prefetch queue."""
    return copernicus_prefetch.get_status(id)


@app.get("/api/overlay")
def api_overlay(id: str, var: str, latest: bool = False):
    """Surface overlay (chla/temp/salinity/o2/ph/biomass/sla) for a file's bbox.

    Normally a read of the prefetched, on-disk snapshot dated at the glider's
    last fix (see copernicus_prefetch). If it isn't stored yet (file still
    processing, or the user clicked before the prefetch reached it) the layer
    is fetched now and stored for next time. `latest` is the map's "Latest"
    toggle: current conditions for any file, fetched on demand and re-fetched
    only once Copernicus has a newer day.
    """
    if var not in copernicus_fetch.OVERLAYS:
        raise HTTPException(status_code=404, detail=f"Unknown overlay '{var}'")
    data = (copernicus_prefetch.get_latest_bytes if latest else copernicus_prefetch.get_layer_bytes)(id, var)
    if data is None:
        data, err = copernicus_prefetch.fetch_layer(id, var, latest=latest)
        if err is not None:
            return err   # plain JSON error (the rare fallback path)
    # Packed binary (uint32 header len + JSON header + raw LE float32
    # lat/lon/val) so the browser skips JSON.parse of a ~100k-element list.
    return Response(content=data, media_type="application/octet-stream")


@app.get("/api/currents")
def api_currents(id: str, latest: bool = False):
    """Surface current (uo/vo) grid for a file's bbox, for the animated flow
    layer. Same store-first behaviour and date rules as /api/overlay."""
    data = (copernicus_prefetch.get_latest_bytes if latest else copernicus_prefetch.get_layer_bytes)(id, "currents")
    if data is None:
        data, err = copernicus_prefetch.fetch_layer(id, "currents", latest=latest)
        if err is not None:
            return err
    return Response(content=data, media_type="application/json")


@app.get("/api/overlay_latest_date")
def api_overlay_latest_date(var: str):
    """Day a "Latest" fetch of this layer resolves to now — a metadata-only
    check the map polls to know when to reload the layer it is showing."""
    if var != "currents" and var not in copernicus_fetch.OVERLAYS:
        raise HTTPException(status_code=404, detail=f"Unknown overlay '{var}'")
    return {"date": copernicus_prefetch.latest_date(var)}


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
