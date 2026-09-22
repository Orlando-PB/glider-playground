# 3D models

Minimal low-poly meshes for the 3D view (`3d_three.html`, mission page; loaded by `ocean3d/models.js`), one JSON file per
object. No textures, no external formats — small enough to inline or fetch
per view.

## Format

`{ partName: {x, y, z, i, j, k, color} }` — each part is a Plotly `mesh3d`
(vertex arrays + triangle index arrays + one colour). The 3D view merges all
parts into a single flat-shaded `mesh3d`, colouring triangles per part via
`intensitymode: 'cell'` and a discrete colourscale.

Conventions: metres, true scale, coordinates rounded to 3 dp.

**Vehicles** (`+x` nose, `y` across the wings, `+z` up, origin near mid-body):

| File | Size | Notes |
|---|---|---|
| slocum.json | 1.47 × 0.22 m, 1.04 m span | parts `hull`, `fins` |
| seaglider.json | 1.8 × 0.30 m, 1.0 m span, 1 m mast | `hull`, `fins` |
| alr.json | 3.6 × 0.8 m | Autosub Long Range; `hull`, `fins` |
| spray.json | 2.0 × 0.20 m, 1 m span | `hull`, `fins` |
| seaexplorer.json | 2.0 × 0.25 m, 0.57 m span, 0.7 m antenna | `hull`, `fins` |
| autonaut.json | 5 m hull | surface vehicle; several parts |
| rrs_discovery.json | 99.7 × 18 m, draft 6.5 m | ship; several parts |
| rrs_james_cook.json | 89.5 × 18.6 m, draft 5.5 m | ship; several parts |
| rrs_sir_david_attenborough.json | 128.9 × 24 m, draft 7.5 m | ship; several parts |

**Vertical objects** (`+z` up):

| File | Size | Origin |
|---|---|---|
| argo.json | 1.27 × 0.165 m hull, 0.69 m antenna | hull mid |
| buoy_red.json / buoy_green.json / buoy_blue.json | 1.1 m body, 1.28 m tall | waterline |
| mooring.json | 6 m tall (display size) | seabed |

## In use

Only the gliders/ALR are wired up today: `ocean3d/models.js` picks
`slocum` / `seaglider` / `alr` from the file's detected platform kind and
poses it along the track (heading from the track, pitch/roll from the file
when present). Everything else is here for future layers (Argo floats, ships,
moorings) and is loaded the same way — `fetch('/static/3d_view/models/<name>.json')`
then merge parts.

Placing an object: rotate vertices by roll (about x), pitch (about y), then
heading (about z); translate; keep `i/j/k` unchanged and only update
`x/y/z` per frame.

## Regenerating

The meshes come from a small Python generator (surfaces of revolution with
12–16 segments, flat quads for fins, triangular prisms for rods) kept outside
the repo. Sources: UW Seaglider spec sheet; NOC / Phillips et al. 2023 for
ALR; NOC and BAS ship particulars. Fin/wing shapes are visual
approximations, not engineering drawings.
