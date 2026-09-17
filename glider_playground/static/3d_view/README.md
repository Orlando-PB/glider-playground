# 3D view assets

Everything that belongs only to the 3D dive-track view (`../3d_view.html`)
lives here, kept apart from the science-facing pages.

- `models/` — low-poly vehicle/object meshes (gliders, ALR, Argo float,
  buoys, ships, mooring). Loaded by `3d_view.html` as
  `/static/3d_view/models/<name>.json`. See `models/README.md` for the format.
- `scenery/` — **decorative only**: plants and sea life scattered over the
  bathymetry and through the water column behind the "Scenery" toggle in the
  view's Style panel (default on; the toggle also switches the seabed's depth shading).
  - `scenery.js` — placement rules per kind (seabed depth band / land /
    floating depth band, plus a latitude band or ocean-region boxes — kelps,
    seagrasses and corals are per-region species), seeded scatter, merge into one
    flat-shaded `mesh3d`, and a gentle sway/bob for underwater things during
    playback. Exposed as `window.Scenery`
    (`load`, `build`, `sway`).
  - `*.json` — the meshes (same format as `models/`; origin at ground level for things that stand, at body centre for things that swim). Species (whales, dolphins, sharks, schooling fish, jellyfish, rays, turtles, eels, octopuses, squid, crabs, lobsters, shells, starfish, urchins, anemones, kelps, corals…) are built by small parametric functions in the generator, one file each; a part named `hull` sets the length the view sizes the animal by.
  - `make_models.py` — regenerates those JSON files; edit shapes here, run
    `python make_models.py`.

Hooks in `3d_view.html` are minimal: the script tag, the `scenery` entry in
`EFFECTS`, the `Scenery.load(...)` call, the `Scenery.build(...)` block at
the end of `buildScene`, and `pushScenery` in the playback tick. Remove those
and the scenery is gone.
