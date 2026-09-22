# 3D model assets

Meshes for the three.js views (`../3d_three.html` and the mission page), which
are drawn by the modules in `../ocean3d/`. The Plotly pages that used to live
here (`3d_view.html`, `scene_kit.js`, `scenery/scenery.js`) are gone.

- `models/` — low-poly vehicle/object meshes (gliders, ALR, Argo float,
  buoys, ships, mooring), loaded by `ocean3d/models.js` as
  `/static/3d_view/models/<name>.json`. See `models/README.md` for the format.
- `scenery/` — **decorative only**: the wildlife behind the "Wildlife" toggle.
  - `*.json` — the meshes (same format as `models/`; origin at body centre for things that swim), built by small parametric functions in the generator, one file each. Only the swimmers are used now (`ocean3d/species.js` says which, where, and who eats whom; `ocean3d/scenery.js` places and animates them); the seabed life and plants are still generated but unused.
  - `make_models.py` — regenerates those JSON files; edit shapes here, run
    `python make_models.py`.
  - `_bundle.json` — every model in one file, written by `make_models.py`; this is what the view
    actually fetches (one request). Re-run the generator after editing or adding a model.
