# Glider Playground — Overview

Internal reference for anyone looking at this codebase. Covers what the
app is made of, how it processes data, how it ships, and how the server deployment works.

## Contents

- [What this is](#what-this-is)
- [Running it / CLI](#running-it--cli)
- [Plotting & rendering libraries](#plotting--rendering-libraries)
- [Data preprocessing pipeline](#data-preprocessing-pipeline)
- [Binary wire format](#binary-wire-format)
- [Server mode (`IS_SERVER`)](#server-mode-is_server)
- [Admin page & analytics plugin](#admin-page--analytics-plugin)
- [Fetching OG1 files from ERDDAP/BODC](#fetching-og1-files-from-erddapbodc)
- [Copernicus Marine overlays](#copernicus-marine-overlays)
- [Share button](#share-button)
- [Advanced diagnostic logging](#advanced-diagnostic-logging)
- [PyPI publishing](#pypi-publishing)

## What glider-playground is

A browser-based explorer for ocean glider NetCDF data (OG1 format). A Python/FastAPI
backend (`glider_playground/app.py`) serves cached, pre-processed data; a JS/HTML frontend 
renders plots in a draggable multi-panel workspace (`glider_playground/static/index.html`).

Ships two ways:
- `pip install glider-playground` → `glider-playground` CLI (`cli.py`), for a single user running
  it locally.
- The public instance at **glider-playground.co.uk**, currently running on a Raspberry Pi in "server mode"
  for shared/multi-user access.

## Running it / CLI

```bash
pip install -e .              # install from source, editable
glider-playground              # run the web server, opens a browser tab on :8420
glider-playground --help       # see all flags and the env vars that configure it
glider-playground --port 9000 --no-browser
glider-playground --version
```

`cli.py` uses stdlib `argparse`. Flags are minimal (`--port`, `--no-browser`,
`--version`) — almost all configuration is via environment variables, listed in `--help`:

| Env var | Effect |
|---|---|
| `IS_SERVER` | Force server mode (see below). Auto-detected on hostnames `raspberrypi`/`server`/`server.local`. |
| `GP_DATA_DIR` | Directory scanned/used for `.nc` files. |
| `GP_PLUGINS_DIR` | Directory of server-only plugin `.py` files (default `~/.glider_playground/plugins`); only loaded when `IS_SERVER`. |
| `LOW_MEMORY_MODE` | Reduce in-RAM preload / point budgets. Currently auto-set `true` when `IS_SERVER`. |
| `DIAGNOSTICS_MODE` |  `DEBUG` logging. |

On startup, `cli.py` also pings PyPI in a background thread and prints a one-line nudge if a newer
version is installable (`cli.py:_check_for_update`).

## Plotting & rendering libraries

- **Plotly** is used for plotting, and it's **vendored**, 
  sitting directly in `glider_playground/static/`:
  - `plotly-gl2d-2.32.0.min.js` — 2D/WebGL build, used by `main_plot.html`.
  - `plotly-gl3d-2.32.0.min.js` — 3D/WebGL build, used by `3d_view.html`.
- The globe in `map_view.html` and the bathymetry/dive-track view in `3d_view.html` are custom
  implementations.
- Shared JS helpers used across the panel pages: `cycle_profile.js`, `console_log.js`.

## Data preprocessing pipeline

Core idea: **a file is processed once on upload** (until the
file changes on disk or `CACHE_VERSION` bumps).

1. A file's identity is `sha256(absolute_path)[:16]`; its content signature is `(size, mtime_ns)`
   — changing the file on disk invalidates its cache automatically.
2. First time a file is seen, `cache_logic.py` runs it through a pipeline of resumable steps
   (tracked so a crash mid-processing resumes rather than restarts):
   1. **Preload** — variable arrays loaded into RAM (or disk-backed in low-memory mode).
   2. **Derive** (`derive_logic.py`) — TEOS-10 salinity/density/conservative temperature via
      `gsw`, plus scientific dive phases and profile numbers from PRES, and QC flags.
   3. **Dataset info** — variable/metadata listing.
   4. **Profiles** (`cycle_profile_logic.py`) — cycle number / `SCI_PHASE` / direction, for the
      profile navigator.
   5. **Spatial** (`spatial_logic.py`) — shared QC'd lat/lon/pres/temp arrays backing both the map
      and 3D view. Falls back through position variable names: `LATITUDE`/`LONGITUDE` →
      BODC `ALATPT01`/`ALONPT01` → `LATITUDE_GPS`/`LONGITUDE_GPS`.
   6. **3D** — dive-track payload.
   7. **Plot prewarm** — default plot payloads pre-computed into the binary plot
      cache so the first click is fast.
3. Everything downstream (`plot_logic.py`'s `/api/plot_data`, map/3D endpoints) reads from this
   cache instead of re-opening NetCDF files.
4. Persistence: registry at `~/.glider_playground/registry.json`, per-file payload sidecars at
   `~/.glider_playground/payloads/<file_id>.json`, plot cache blobs at
   `~/.glider_playground/plotcache/`.

**`CACHE_VERSION`** (top of `cache_logic.py`) is folded into every cache key. **Bump it whenever a
processing change alters cached output**. this forces full reprocessing of every registered file on next startup and
wipes the stale plot cache, instead of silently serving old results.

## Binary wire format

Designed by claude

Numeric arraysare shipped from backend to browser as a
custom binary container to avoid `JSON.parse`/`JSON.stringify` cost on large
arrays and roughly halves payload size.

- **Format**: `uint32 LE header length` → `JSON header` (scalar metadata + an `arrays` descriptor
  of `{dtype, len}` per array, dtypes `f64`/`f32`/`u8`) → each array's raw little-endian bytes
  concatenated in the declared order. NaN is preserved as NaN (not `null`) since Plotly's
  `scattergl` treats NaN as a legitimate gap in a line.
- Server-side packer: `plot_logic.py`'s `_pack_plot_binary` (also used by `overlay_logic.py` for
  Copernicus overlay data).
- Client-side unpacker: `parsePlotBinary()` in `main_plot.html` — reads the header length via
  `DataView.getUint32`, JSON-parses the header, then slices `Float64Array`/`Float32Array`/
  `Uint8Array` views directly out of the response `ArrayBuffer` (no copy-then-parse). Datetime
  x-values are packed as epoch-ms `f64` and converted client-side to Plotly's expected date-string
  format.
- The plot fetch requests this path explicitly with `?binary=1` and branches on
  `Content-Type: application/octet-stream` vs JSON (JSON responses are the error/fallback case).
- Separately, `plot_logic.py` also uses `.npy` files for **on-disk** array persistence when
  low-memory mode is active — this is a different thing from the binary-over-HTTP format above;
  it's the disk-backed cache tier, not the wire format.
- Non-binary JSON responses still get a size-reduction pass: floats are trimmed to 7 significant
  figures before serializing.

## Server mode (`IS_SERVER`)

Single source of truth: `server_config.IS_SERVER`, read from the `IS_SERVER` env var, set once by
`cli.py` at startup (either because the env var was already `"True"`, or because the hostname
matches `raspberrypi`/`server`/`server.local`). Setting it also forces `LOW_MEMORY_MODE=true` and
binds `0.0.0.0` instead of `127.0.0.1`.

What changes when it's on:

- Background processing (cache building) is deprioritized: worker thread reniced, throttling
  sleeps inserted between pipeline steps, and lower point budgets (60k prewarm points vs 100k,
  1000 map/3D points vs 5000) — so heavy numpy work doesn't starve `uvicorn` serving other users.
- **Multi-user safety**: file deletion (`DELETE /api/files/{file_id}`), the native file/folder
  picker, and the Copernicus Marine login endpoint are all **disabled** server-side — none of
  these make sense when multiple people share one instance.
- **Server-only plugins** are loaded from `~/.glider_playground/plugins/*.py` (or
  `GP_PLUGINS_DIR`) — this is how the admin/analytics page gets added; see below. Pip/local
  installs never load this directory.

## Admin page & analytics plugin

This was a temporary fix to access some analystics and change files remotely on my Pi server.

**`/admin/stats` is not defined in `app.py`.** It's added entirely by a server-only plugin,
`deploy/analytics.py`, loaded by `app.py`'s `_load_server_plugins()` only when `IS_SERVER` is true.
That loader execs every `.py` file in the plugins directory and calls its `register(app)` function.

- The plugin's source lives in this repo's `deploy/` directory, which is **gitignored — "never
  publish"** per its own header comment. On the Pi, the mirrored copy sits at
  `~/.glider_playground/plugins/analytics.py`. It is not included in the PyPI package (package
  data only covers `glider_playground/static/*`).
- Routes it adds: `POST /api/track` (fire-and-forget beacon; a background thread writes to SQLite
  off the request path), `GET /api/admin/stats` (JSON), `GET /admin/stats` (HTML dashboard).
- **Auth**: HTTP Basic, gated by `GP_STATS_USER`/`GP_STATS_PASS` env vars (set on the Pi via
  systemd, not in the repo). No `GP_STATS_PASS` set → dashboard returns 503 (disabled by default
  until configured). I'm aware this is not very secure at all.
- **Data**: pageviews, unique/returning visitors, device/browser/OS/country, top events,
  time-on-page — from `~/.glider_playground/analytics.db` (SQLite, WAL mode). No IP storage, no
  cookies — visitor id is a random first-party id in browser `localStorage`.
- Two sibling plugins share the same auth: `deploy/waypoints_admin.py`, `deploy/file_admin.py`.

## Fetching OG1 files from ERDDAP/BODC

This can be fully overhauled.

`live_logic.py` scans BODC's ERDDAP files index at `https://linkedsystems.uk/erddap/files/` for
NetCDF files updated in the last `DAYS_ACTIVE` (7) days, matching suffix `_R.nc` ("real-time" OG1
files), and downloads them into `DATA_DIR`.

- **Discovery**: fetches ERDDAP's `.json` directory-listing format, walks dataset directories
  modified recently or named `*_R`, and keeps recently-modified `_R.nc` files. The scan result is
  cached in-process for `SCAN_CACHE_TTL` (120s) so concurrent viewers share one upstream fetch.
- **Download**: streamed to a `.part` temp file, then atomically renamed into `DATA_DIR`. Downloads
  are serialized through a single-worker thread pool (one at a time) to avoid hammering ERDDAP or
  disk I/O.
- **Ownership tracking**: a marker file (`.glider_playground_managed.json` in `DATA_DIR`) records
  every file `live_logic` itself downloaded, with its server mtime and download time. Auto-prune
  and auto-update **only ever touch files present in this marker** — manually uploaded/placed
  files are never deleted or overwritten by the live-fetch system.
- **Suppression**: a separate marker (`.glider_playground_suppressed.json`) tracks gliders a user
  explicitly removed via the UI; auto-download skips them until the user explicitly re-requests
  the file, which clears the suppression.
- **Auto-update sweep**: rate-limited to once per `AUTO_UPDATE_COOLDOWN` (5 min), runs as a side
  effect of any live-feed fetch and also from a dedicated background thread every
  `SCANNER_INTERVAL` (30 min) so gliders update even with the Files panel closed. It downloads new
  files, re-downloads files where the server's copy is newer, and deletes managed files
  once they fall outside the `DAYS_ACTIVE` window.

## Copernicus Marine overlays

I'm not sure how licensing for this works. Initially I built it just for personal use.

`overlay_logic.py` fetches satellite/model fields from Copernicus Marine (via the
`copernicusmarine` Python toolbox) and draws them as coloured cells/particles on the
`map_view.html` globe — chlorophyll, temperature, salinity, oxygen, pH, phytoplankton
biomass, sea-level anomaly, and surface currents.

- **Auth**: the toolbox stores credentials in a single file, `~/.copernicusmarine/.copernicusmarine-credentials`,
  independent of the Python env or process — a login done anywhere is visible everywhere.
  `POST /api/copernicus/login` (`app.py`) validates username/password online and persists them via
  `overlay_logic.login()`; no restart needed since `open_dataset` reads the file per call.
  `GET /api/copernicus/status` just checks the file exists. The login endpoint is **disabled in
  server mode** — doesn't make sense for a shared multi-user Pi instance.
- **Registry** (`OVERLAYS` dict): each overlay maps a key (`chla`, `temp`, `salinity`, `o2`, `ph`,
  `biomass`, `ssh`) to one or more candidate Copernicus dataset ids plus a variable name. `chla` and
  `ssh` are 2D L4 satellite/altimetry products; the rest are 3D model analysis/forecast products
  from which only the shallowest depth level is pulled (`surface: True`). `ssh` also sets
  `demean: True` — the SLA field carries a basin-scale offset, so the box's spatial mean is
  subtracted before display, matching how Copernicus users normally plot it, and it's shown on a
  fixed ±0.2 m scale rather than the data's own percentile range. **Currents** (`uo`/`vo` from one
  analysis/forecast dataset) are a vector field, handled by a separate fetch/extract path so the
  frontend can bilinearly interpolate a continuous flow for animated particle advection.
- **Fetch flow** (`fetch_overlay`/`fetch_currents` → `_fetch_cached`): pads the glider's bbox out to
  a ~12° box (cos-latitude-widened so it's physically square, not square-in-degrees, capped at 16°
  half-span), defaults to 2 days before "now" if no date is given (satellite/model latency), and
  keys a 24-entry session LRU cache (`_CACHE`) on `(var, rounded bbox, date)` so panning slightly or
  re-toggling an overlay doesn't re-hit Copernicus. `_try_datasets` walks each candidate dataset in
  order, retrying at the dataset's actual max date if the requested date is out of range, and
  classifies auth failures (401/403/"credentials"/etc.) separately from other errors so the frontend
  can show a login prompt vs. a generic error.
- **Grid extraction** (`_extract`/`_extract_vec`): subsamples adaptively so the returned grid stays
  under `_MAX_CELLS_PER_SIDE` (800; 160 for the coarser currents grid) cells per axis — the satellite
  CHL-a grid is native ~4 km resolution, so a typical 12°-ish box stays at full resolution. Scalar
  overlays flatten to `[lat, lon, value]` points with p10/p90 for the colour ramp; currents keep
  their `nlat x nlon` grid shape (masked cells → `null`) plus speed p90/max.
- **Wire format**: scalar overlay responses are packed by `pack_overlay_response()` into the same
  binary container used for plot data (see [Binary wire format](#binary-wire-format)) — lat/lon/val
  as raw float32 arrays instead of a large JSON list of triples. Currents responses stay JSON (grid
  shape, not a flat point list).
- `_overlay_target_date` (`app.py`) picks the date to request per file: the glider's last data point
  for a past deployment, or "now" (capped server-side to the dataset's latest day) for an active one.
- `warm_up()` pre-imports `copernicusmarine` in a background thread at startup so the first overlay
  request of a session doesn't pay its ~2s cold-import cost inline.

## Share button

The share button copies a URL that reconstructs the exact current view (file, layout, theme,
time-axis alignment) for whoever opens it — implemented entirely in `index.html`.

- Payload (`buildSharePayload()`): `{ v: SHARE_VERSION, file, fileKey, fileName, live, align,
  theme, layout }` — `file` is the exact local file id (fast path when both people share the same
  machine/server); `fileKey` is a portable filename fallback; `live` flags a BODC live glider so
  the recipient can auto-download it if they don't have it locally; `layout` is the full
  draggable multi-panel dashboard layout (same shape that's persisted to `localStorage` normally).
- The payload is JSON-stringified, URL-safe-base64-encoded, and placed in a `?s=` query param on
  the current URL.
- On load, `readShareFromUrl()` decodes `?s=`, checks the version matches, stashes it, and
  immediately scrubs the param from the address bar (`history.replaceState`) so a page reload
  doesn't keep re-applying the same share link.
- `resolveSharedFile()` then tries to match the shared file by exact id, then by portable
  filename, then — if `live` was set — falls back to auto-downloading it from BODC. A share link's
  layout takes priority over both the recipient's saved dashboard state and the default boot
  state.

## Advanced diagnostic logging

Two independent layers.

- **Backend** (env var): set `DIAGNOSTICS_MODE=true` before launch. This sets the
  `glider_playground` logger tree to `DEBUG` (default is `INFO`) and turns up the third-party
  `copernicusmarine` logger too. Output goes to stdout/the server log, format
  `%(asctime)s - %(levelname)s - %(message)s`.
- **Frontend** (browser devtools, no UI toggle or URL param): in any panel's devtools console, run
  `gpSetDebug(true)` (or `false` to turn off). This flips `window.GP_DEBUG`, backed by
  `localStorage['gp_debug']`. Since all panels are same-origin iframes sharing `localStorage`, the
  toggle applies instantly across the whole workspace. When on, logs verbose API timing, redraw
  timing, render timing, and full plot-render phase breakdowns to the browser console.

## PyPI publishing

Handled by `.github/workflows/publish.yml`, triggered on **every push to `main`** — there is no
manual release step.

1. A Python one-liner in the workflow regexes the `version = "X.Y.Z"` line out of `pyproject.toml`,
   bumps the **patch** number, and rewrites the file.
2. The bumped `pyproject.toml` is committed straight back to `main` as `github-actions[bot]`
   and pushed using the workflow's own `GITHUB_TOKEN`.
3. `python -m build` builds the sdist/wheel, then `pypa/gh-action-pypi-publish` uploads to PyPI
   using a `PYPI_API_TOKEN` secret.
