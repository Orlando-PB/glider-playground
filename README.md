# Glider Playground

<a href="https://www.noc.ac.uk/" title="National Oceanography Centre"><img src="glider_playground/static/readme_images/NOC_logo.svg" alt="National Oceanography Centre" width="72" align="right"></a>

A fast, web-based viewer for oceanographic data. Load OG1 NetCDF files — or pull live deployments from BODC.

**Live demo:** [glider-playground.co.uk](https://glider-playground.co.uk) *(running on a Raspberry Pi — may be slow)*

![Glider Playground](glider_playground/static/readme_images/whole_view.webp)

---

## Install & Run

```bash
pip install glider-playground
glider-playground
```

This opens the app in your browser. Press `Ctrl+C` in the terminal to stop it.

<details>
<summary>Install from source</summary>

```bash
git clone https://github.com/Orlando-PB/glider-playground.git
cd glider-playground
pip install .
```
</details>

---

## Loading Data

<img src="glider_playground/static/readme_images/files_dropdown.webp" alt="Files panel with live BODC deployments" width="340" align="right">

Click the **file button** (top-left) to open the file panel. You can:

- **Browse live BODC deployments** — pull published datasets directly from the British Oceanographic Data Centre
- **Add files** — pick individual `.nc` files
- **Add folder** — load an entire folder of `.nc` files at once

Your own files are processed once in the background. Once ready, click a file to load it.

> **File format:** input files must be in [OG1](https://github.com/OceanGlidersCommunity/OG-format-user-manual) format (or OG1-compatible) — the OceanGliders community NetCDF standard.

> Live data is provided by the [British Oceanographic Data Centre (BODC)](https://platforms.bodc.ac.uk/deployment-catalogue/).

---

## Views & Layout

Glider Playground is a flexible, multi-panel workspace — plots, the globe, and the 3D track are all panels you can arrange however you like.

- **Drag** a panel by its header to reorder or swap it with another
- **Split** any panel (the edge **+** buttons) to add another plot or map beside it
- **Resize** by dragging the dividers; **close** a panel with its ✕

### Views

The **View** bar rebuilds the whole workspace in one click:

| View | What you get |
|---|---|
| **Classic** | A single plot with the globe and 3D track (the default) |
| **Map** | Globe + 3D view side by side, no plot |
| **Overview** | Globe + 3D alongside the headline plots |
| **Dashboard** | Globe + 3D + the six core plots, each with a depth-profile sidebar |
| **Duo** | Two globes (chlorophyll + currents overlays) beside backscatter and salinity plots |
| **Bio-optics** | Temperature, chlorophyll, backscatter and PAR plots with a full-height globe |
| **Stats** | Deployment summary, map, instruments, and searchable variable / attribute tables (derived variables are labelled) |

![Dashboard view — globe, 3D track, and six plots with profile sidebars](glider_playground/static/readme_images/dashboard.webp)

![Bio-optics view — temperature, chlorophyll, backscatter and PAR beside a full-height globe](glider_playground/static/readme_images/bio_optics.webp)

![Stats view — deployment summary, instruments, variables and attributes](glider_playground/static/readme_images/stats.webp)

### Presets

The **Presets** row sets what a plot shows: **Phases, Thermal, T-S Diagram, Salinity, Density, Chlorophyll, Oxygen, Backscatter, PAR**. Presets the current file has no data for are greyed out.

### Globe & Overlays

- **Globe** — the glider's GPS track on an interactive 3D globe, with every other loaded deployment shown faintly alongside
- **Copernicus overlays** — drape satellite/model surface fields over the globe: **Chlorophyll-a, Temperature, Salinity, O₂, pH, Biomass, Sea Level Anomaly**, plus **Currents** as an animated particle-flow field. **Smooth** toggles interpolation of the overlay grid, and **Latest** swaps the deployment-date snapshot for today's conditions
- **Glider DAC** — per-dive depth-averaged current vectors, shown when the file provides them
- **Argo floats** — latest position of every Argo float, with details on click
- **Research ships** — latest reported positions of RRS Discovery, RRS James Cook and RRS Sir David Attenborough

![Duo view — chlorophyll and surface-current overlays beside backscatter and salinity plots](glider_playground/static/readme_images/globe_overlay.webp)

### 3D View

The dive track drawn in 3D over NOAA bathymetry. Height is scaled automatically.

<img src="glider_playground/static/readme_images/3d_view.webp" alt="3D view — a dive track over bathymetry, with scenery" width="520" align="right">

- **Playback** — press play (or drag the slider) to fly the vehicle along its track, at a choice of speeds
- **Style** menu:
  - **Colour land** — shade land and ice above the waterline
  - **True height** — turn off the vertical scaling
  - **Scenery** — shows cosmetic details
  - **Argo floats** — show floats that surfaced near the glider during playback

<br clear="right">

![Map view — globe layer menu and the 3D view's Style menu](glider_playground/static/readme_images/map_layers.webp)

### Copernicus Setup

The surface overlays and currents are fetched live from [Copernicus Marine](https://marine.copernicus.eu/), which needs a (free) account and a one-time login:

1. **Register** for a free account at [marine.copernicus.eu/register](https://data.marine.copernicus.eu/register).
2. **Install** the toolbox:
   ```bash
   pip install copernicusmarine
   ```
3. **Log in** (stores your credentials locally), then restart Glider Playground:
   ```bash
   copernicusmarine login
   ```

Once you're logged in, the overlay layers fetch on demand. Until then the app will prompt you with whichever of these steps is missing.

Overlays and currents are provided by the E.U. Copernicus Marine Service:

- Global Ocean Physics Analysis and Forecast. E.U. Copernicus Marine Service Information (CMEMS). Marine Data Store (MDS). DOI: [10.48670/moi-00016](https://doi.org/10.48670/moi-00016)
- Global Ocean Biogeochemistry Analysis and Forecast. CMEMS, MDS. DOI: [10.48670/moi-00015](https://doi.org/10.48670/moi-00015)
- Global Ocean Colour L4 (NRT / Multi-Year). CMEMS, MDS. DOI: [10.48670/moi-00279](https://doi.org/10.48670/moi-00279), [10.48670/moi-00281](https://doi.org/10.48670/moi-00281)
- Global Ocean Sea Level L4 (NRT / Multi-Year). CMEMS, MDS. DOI: [10.48670/moi-00149](https://doi.org/10.48670/moi-00149), [10.48670/moi-00148](https://doi.org/10.48670/moi-00148)

---

## Plotting & Inspecting

By default, presets drive the plots. Open **Settings** (top bar) to change the plot variables and more.

![Settings bar](glider_playground/static/readme_images/settings.webp)

- **Zoom** — drag a box on the plot to zoom in; double-click to reset
- **Colour bar slider** — drag the ends of the colour bar to trim the colour range (**Auto** / **Reset** beneath it)
- **Inspector** — hover the plot to read exact values for the nearest sample in a floating card
- **Profile sidebar** — the **Profile** button adds a value-vs-depth plot to the left of a plot
- **Order / Size / Quality** — choose which points draw on top, marker size, and the maximum number of points drawn
- **Colour palette** — pick from a range of oceanographic colour maps
- **Phases** — show only selected glider phases
- **Sync time** — zooming one plot zooms all
- **Share** — copy a link that reopens this exact view
- **Download** — save a high-resolution PNG of the whole workspace (every panel as shown, without the controls)
- **Dark theme** — toggle from the top bar

![Overview in the dark theme](glider_playground/static/readme_images/dark_theme.webp)

### Profiles

If a file contains dive profiles, a **Profiles** navigator appears. Step through individual profiles or cycles, filter by direction (upcast / downcast / transect), or view everything at once.

---

## Quality Control

QC flags follow the Argo convention: `0` No QC, `1` Good, `2` Probably good, `3` Probably bad, `4` Bad, `5` Changed, `8` Interpolated, `9` Missing. Samples are always filtered to the allowed set (default `0,1,2,5,8`).

- PRES gaps up to 5 minutes are always interpolated (flag `8`); a longer gap is a real data gap and is left unfilled.
- Exact `0.0` fill values in PRES/TEMP/CNDC are always flagged missing (flag `9`) and nulled.
- NaT and out-of-order timestamps are always dropped outright. Other out-of-range timestamps (pre-1990 / future) are flagged bad (flag `4`) and filterable like anything else.

---

## Uninstall

```bash
pip uninstall glider-playground
```

---

## Developer Info

To change the default plots, dashboard views or colour palettes, edit [`glider_playground/plot_presets.json`](glider_playground/plot_presets.json) (instructions are at the top of the file) and restart.

See [OVERVIEW.md](OVERVIEW.md) for the code layout, architecture, data pipeline, and deployment.

---

## License

Licensed under the [Apache License 2.0](LICENSE).

<a href="https://www.noc.ac.uk/" title="National Oceanography Centre"><img src="glider_playground/static/readme_images/NOC_logo.svg" alt="National Oceanography Centre" width="56" align="left"></a>

Developed by **Orlando Prugel-Bennett** at the **[National Oceanography Centre (NOC)](https://www.noc.ac.uk/)**.

© 2026 National Oceanography Centre.
