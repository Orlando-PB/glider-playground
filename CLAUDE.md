# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Glider Playground — a local-first, browser-based explorer for ocean glider NetCDF data (OG1 format). Python/FastAPI backend serves cached, pre-processed data; a vanilla-JS/HTML frontend (no build step, no framework) renders plots, a 3D globe, and a 3D dive-track view in a draggable multi-panel workspace. Ships two ways: `pip install glider-playground` (`cli.py`) and a public instance at glider-playground.co.uk running on a Raspberry Pi.

## Commands

```bash
pip install -e .              # install from source, editable
glider-playground              # run the web server (cli.py), opens browser on :8420
```

There is no test suite, linter, or build step configured. Frontend is plain HTML/CSS/JS served directly from `glider_playground/static/` — no bundler, no npm install needed to run the app. `tailwind/` holds the Tailwind config/input used to regenerate `static/tailwind.css` (no automated watch script — check `tailwind/` before assuming it's wired into any build).

`publish.yml` handles PyPI publishing.

## Architecture

**Backend (`glider_playground/`)** — FastAPI app (`app.py`) exposing `/api/*` endpoints, all data-shaped around one idea: a file is processed once and reused.

- `cache_logic.py` — the core cache. Each registered file gets a content signature (size+mtime); on first sight it's fully processed (map/3D/profile/variable data precomputed, arrays preloaded into RAM) and the registry persists to `~/.glider_playground/registry.json`. Everything else reads from this cache instead of re-opening NetCDF files. **`CACHE_VERSION`** (top of the file) is folded into every cache key — bump it whenever a processing change alters cached output (e.g. new/changed derived vars, plot decimation, position resolution) so all files get reprocessed on next startup instead of serving stale cached results. Keep the versioned changelog comment above it up to date.
- `plot_logic.py` — plot data extraction/downsampling for the main plot endpoints; also hosts the "derived store" that `derive_logic.py` writes into.
- `derive_logic.py` — runs once per file after preload; derives TEOS-10 salinity/density/conservative temp via `gsw`, plus scientific phases/profile numbers from depth.
- `spatial_logic.py` — shared QC'd lat/lon/pres/temp arrays that back both the map and 3D view (cached per file).
- `cycle_profile_logic.py` — cycle number / SCI_PHASE / direction metadata and the profile navigator.
- `overlay_logic.py` — Copernicus Marine overlays (chlorophyll, temp, salinity, O2, pH, biomass, SSH) and surface currents; requires local `copernicusmarine login`.
- `live_logic.py` — pulls live BODC/ERDDAP deployments into local `DATA_DIR`, tracks which files it owns (vs. user-added) so auto-prune/update never touches user files. Designed for the shared Pi deployment (in-process scan cache, serialized downloads, rate-limited auto-update).
- `update_logic.py` — checks installed version against PyPI and tailors upgrade instructions to how the app was installed (git/pip).
- `cli.py` — the front door; boots the `app.py` FastAPI/uvicorn server and opens the browser.

**Frontend (`glider_playground/static/`)** — no framework, no build step.

- `index.html` (~6.5k lines) — the shell: file panel, dashboard presets, the draggable/splittable multi-panel workspace (`panels` map keyed by panel id), and the globe. Panels of type `plot`/`map`/`3d` are `<iframe>`s pointed at the other static pages; the shell talks to them via `postMessage` (zoom sync, multiview state, etc.), not shared JS state.
- `main_plot.html` — a single plot panel (Plotly-based); one iframe instance per plot panel.
- `map_view.html` — the interactive globe (track, Copernicus overlays, current particle field, DAC arrows).
- `3d_view.html` — the 3D dive-track + bathymetry view; a singleton panel (only one instance, unlike plots/maps which can multiply).
- `cycle_profile.js`, `console_log.js` — shared helpers pulled into the above pages.
- `plotly-gl2d-*.min.js` / `plotly-gl3d-*.min.js` — vendored Plotly builds (large, don't edit).

**Jelly** is a passive notification bubble (chat/AI-driving functionality was removed) — hidden until there's something worth showing (a new release, or Copernicus overlay setup help), then it reveals a bubble with a dot and lists the notes. Not related to Claude Code.

## Notes

- QC handling (apply/highlight/filter-time/interpolate/clean) is a first-class concept threaded through `plot_logic.py`/`spatial_logic.py` and the Advanced UI — see README's Quality Control table before changing QC-flag logic.
- Colour palettes users paste in are typically given top-to-bottom and need reversing before use as a Plotly/CSS gradient (see memory).
- The public deployment auto-deploys from `main` on the Pi; the private analytics plugin lives outside this repo (see `deploy/README-analytics.md`, gitignored).

## The Pi (public deployment)

`glider-playground.co.uk` runs on a Raspberry Pi at home, reachable over SSH as `ssh server` (user `orlando`, passwordless sudo). **`ssh server` only works when I'm on my home internet** — from elsewhere it won't connect, so don't rely on it being reachable.

- Runs as systemd service `glider-playground.service` (`IS_SERVER=True`) from a git checkout at `/home/orlando/glider-playground`, auto-updated from `main` by a 5-min cron (`~/auto_update_glider.sh`). Also hosts a Minecraft server and other unrelated bits.
- Server-only plugins live in `~/.glider_playground/plugins/` (analytics + file manager); source-of-truth mirrored in this repo's gitignored `deploy/`. Admin panel at `/admin/stats` (HTTP Basic).

## Working with me

- Ask before making non-obvious design/architecture decisions — don't silently pick an approach when there's a real tradeoff; let me choose.
- Don't run the app / manually exercise features to "test" a change — describe what to check and let me test it in the browser myself.
- Never push to GitHub, open/merge PRs, or otherwise touch the remote — I do all git publishing manually. Local commits only if I ask, and never `git push`.

## Keeping this file current

If a change alters the architecture described above (new backend module, new frontend panel type, changed data flow, new deployment target), update this file as part of that change. Don't update it for routine bug fixes, small features, or anything not covered above.
