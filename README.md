# Glider Playground

A fast, web-based NetCDF explorer for glider data. Load your `.nc` files and instantly explore, plot, and QC your data — no scripting required.

**Live demo:** [glider-playground.co.uk](https://glider-playground.co.uk) *(running on a Raspberry Pi — may be slow)*

![Glider Playground Home View](glider_playground/static/home_view.png)

---

## Install & Run

```bash
pip install glider-playground
glider-playground
```

This opens the app in your browser. Press `Ctrl+C` in the terminal to stop it.

> **Virtual environment recommended.** If you use one, activate it before running.

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

Click the **file button** (top-left) to open the file panel. From there you can:

- **Add files** — pick individual `.nc` files
- **Add folder** — load an entire folder of `.nc` files at once
- **Download demo data** — grab two example files to try immediately

Files are processed in the background. Once ready, click one to load it.

> Sample data is available from the [BODC Deployment Catalogue](https://platforms.bodc.ac.uk/deployment-catalogue/BIO-Carbon/).

---

## Exploring Data

### Plot
Pick your **X**, **Y**, and optionally a **Colour** variable from the dropbdowns. The plot updates automatically.

Use the **View** menu for built-in presets: Thermal Structure, T-S Diagram, Salinity, Density, Chlorophyll, Oxygen, Backscatter.

### Zoom & Inspect
- **Box zoom** — click and drag on the plot
- **Axis sliders** — trim the X, Y, or colour range precisely
- **Click a point** — see exact values in the sidebar and the glider's GPS position on the map
- **Reset Lims** — return to the full view

### Profiles
If your file contains dive profiles, a profile selector appears in the toolbar. Step through individual profiles or view them all at once.

### 3D View & Map
The right sidebar shows:
- **3D View** — an interactive 3D track with bathymetry
- **Map** — glider GPS track; updates to the clicked position when using the inspector

---

## Quality Control

| Toggle | What it does |
|---|---|
| **Apply QC** | Hides points whose `_QC` flag isn't in the allowed list (default: `0,1,2,5,8`) |
| **Highlight** | Shows bad-QC points in red instead of hiding them |
| **Interpolate** | Fills NaN gaps in PRES/TEMP/CNDC by time (filled points flagged QC=5) |
| **Clean** | Flags zero fill-values, auto-scales CNDC units, cross-flags 5σ CNDC outliers |
| **MLD** | Overlays Mixed Layer Depth (ΔT = 0.2 °C threshold) |

Edit the **QC flags** field to customise which flags count as good.

---

## Other Features

- **Plot All** — disables the 200k-point cap to render every sample
- **Download** — saves the current plot as a PNG
- **Jelly** — an AI assistant (bottom-right bubble); paste your OpenAI key to enable it. The key is saved locally and never leaves your machine.

---

## Uninstall

```bash
pip uninstall glider-playground
```
