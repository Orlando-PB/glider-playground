# Missions (experimental, self-contained)

A **mission** is one 3D scene, in the style of the 3D view (same seabed, water, scenery and models), that holds
several platforms at once: gliders, ALRs, Argo floats and ships. They share one time bar, and the mission's
title, timeline, key and stage labels are laid over the scene. Clicking a platform opens that file in the
normal views; "Back to mission" returns.

Everything lives in this folder. The rest of the app touches it in two places: `missions.attach(app)` in
`app.py` and the `missions_shell.js` script tag at the end of `static/index.html`. Remove those and it's gone.

| File | What it does |
| --- | --- |
| `examples/*.json` | Bundled missions. `biocarbon.json` is the reference example: copy it. |
| `mission_logic.py` | Loads missions, matches platforms to registered files, serves tracks, floats, bathymetry. |
| `template.json` | Starter JSON shown by the list page's "New mission" dialog. |
| `bundle.py`, `__main__.py` | Pack / import mission bundles (`.zip`). |
| `routes.py` | `/api/missions`, `/api/missions/<id>`, `/preview`, `/scene`, `/track/<key>`, `/export`, `/import`, `/template`, `/guide`; the `/missions` pages; mounts `static/`. |
| `static/mission_view.html` + `.js` | The mission list (map thumbnails from `/preview`) and the scene, time bar and labels. Runs in an iframe over the shell's workspace. |
| `static/missions_shell.js` | In the shell: the "Missions" button, the overlay, the `/missions` ↔ `/missions/<id>` URLs (pushState), hiding the presets + Settings while a mission is open, and "Back to mission" after a platform was opened (Classic view). |

User missions go in `~/.glider_playground/missions/<id>.json` (same id as a bundled one overrides it).
`/missions` and `/missions/<id>` serve the normal shell with the mission opened, so a mission is shared as a plain URL.

The key's **Track colour** menu colours every platform's travelled track by a plot preset (temperature, salinity, …)
on one shared scale. Only presets whose variable every loaded platform has are offered; nothing to configure.

## Writing a mission JSON

Dates are naive UTC (`"2024-06-09"` or `"2024-06-09T05:10"`). Positions are decimal degrees. `offset` is
`[right, down]` in screen pixels from the anchor. Unknown keys are ignored, so `_help` / `note` comments are fine.

```jsonc
{
  "title": "BIO-Carbon",                             // \n = line break
  "summary": "One line for the mission list.",
  "time":   { "start": "2024-05-24", "end": "2024-10-01", "open_at": "2024-08-06" },   // time bar range. A mission plays from the start; "open_at" (optional) opens it paused on that date
  "region": { "lat": [54.5, 67.0], "lon": [-32.0, 0.0] },   // scene box. Omit to fit the platforms' tracks.
  "camera": { "eye": [-0.1, -1.12, 1.04], "center": [0.08, 0.12, -0.2] },              // optional, Plotly scene units
  "vertical_exaggeration": 60,                                                         // optional
  "model_scale": 1.0,                                                                  // optional: all vehicle models bigger / smaller

  "platforms": [{
    "key": "alr4",                    // your handle for it; stages/events/ships refer to this
    "label": "ALR4",                  // name tag in the scene
    "file": ["ALR_4_649.nc"],         // filename(s) of the registered .nc, first one found wins; a "<name>_Processed.nc" copy of any of them is preferred
    "model": "alr",                   // alr | slocum | seaglider (static/3d_view/models). Omit to auto-detect.
    "colour": "#12295c", "width": 6,  // track line
    "from": "2024-05-27", "to": "…",  // optional: clip the track (e.g. drop pre-deployment tests)
    "group": "MARS Slocum gliders",   // optional: platforms sharing a group share one model row in the key
    "key_label": "ALR4 · mixed layer, ~20 m", "show_label": true
  }],

  "floats": {                         // Argo floats, from the GDAC index the Argo layer keeps (positions only)
    "wmo": ["3901581"],                                                   // named floats, and / or…
    "deployed_near": { "lat": 60.0, "lon": -24.0, "radius_km": 40,        // …floats whose FIRST profile is
                       "between": ["2024-05-24", "2024-06-05"] },         //    inside this circle + window
    "colour": "#0f8b8d"
  },

  "ships": [{                         // no ship data is read: a ship is a schematic path you describe
    "key": "discovery", "label": "RRS Discovery", "model": "rrs_discovery", "colour": "#e8702a",
    "deploys": ["nelson", "cabot", "floats"],   // ride the aft deck until their first fix ("floats" = every float)
    "recovers": ["alr6", "alr4"],               // ride the deck from their last fix on. Deck holds 4 gliders, 4 floats, 2 ALRs.
    "legs": [
      { "label": "Discovery outbound", "time": ["2024-05-24", "2024-05-28"], "points": [[54.5, -12.8], [60.0, -24.0]] },
      { "time": ["2024-05-28", "2024-06-22"], "follow": ["nelson", "cabot"], "offset_km": 25 }   // stay with platforms
    ]                                 // between legs the ship holds position; before the first it is hidden;
                                      // a last leg ending at the edge of the region sails out of the scene
                                      // (force with "leaves_scene": true | false)
  }],

  "stations": [{ "label": "CIB SUPERSTATION", "lat": 60.0, "lon": -24.0, "radius_km": 70, "colour": "#0f8b8d",
                 "buoy": "blue" }],     // ring on the surface; "buoy" (optional) adds one at its centre: blue | green | red

  "stages": [{                        // navy pills with a leader line
    "title": "Transit to Iceland Basin", "detail": "9–17 Jun · 400 km",
    "at": { "platform": "alr4", "time": "2024-06-13" },    // on that platform's real track at that time…
    "offset": [-230, -10]                                   // …or "at": { "lat": 60.0, "lon": -24.0 }
  }],
  "stage_icon": "alr",                // icon inside the pills (static/icons/<name>-mapicon.svg)

  "events": [{                        // numbered orange dots + the TIMELINE card; they light up as time passes
    "date": "17 Jun", "text": "ALRs reach the gliders", "time": "2024-06-17",
    "at": { "platform": "alr4", "time": "2024-06-17" }, "offset": [-6, -34]
  }],

  "places": [{ "text": "ICELAND", "lat": 64.9, "lon": -18.5, "style": "big" }]   // style: big | sea | (none)
}
```

Tips for whoever (or whatever) writes one:

- Get real dates and positions from the data first (first/last fix, when platforms came within a few km of
  each other, distance per phase), then write stages and events from those. Don't invent ship tracks: describe
  legs only through places and dates something else pins down.
- Pin labels to a platform and time rather than a lat/lon wherever you can; they then sit exactly on the track.
- Start with every `offset` at `[0, -60]`, look at the scene, then nudge the ones that collide.

## Bundles: sharing a mission, or putting it on a server

A bundle is one `.zip` with `mission.json` and the **full** data files (`data/*.nc`, stored uncompressed).

```bash
python -m glider_playground.missions pack biocarbon -o biocarbon.mission.zip
python -m glider_playground.missions import biocarbon.mission.zip     # unpack + register the files
python -m glider_playground.missions list
```

The mission list page does the same on a local install: **New mission** (template + a copyable AI prompt, saved to
`~/.glider_playground/missions/`), **Import mission** (a bundle `.zip` or a bare `.json`), and per mission **Export**
(JSON only) / **Export with data** (the bundle). A server offers neither, and lists only missions placed in its own missions folder (no bundled examples, no New/Import).

Importing never overwrites data: if a file with the same name is already registered, the bundle's copy is only
unpacked when it is newer (by modified time, carried in the bundle's `manifest.json`); the older copy stays where it
is and missions resolve a filename to its newest registered copy.

Locally you can also `POST /api/missions/import` with the zip. On a server that endpoint is off; copy the zip
into `~/.glider_playground/missions/inbox/` (scp, or the admin file manager) and it is imported the next time the
mission list is requested. Data unpacks to `~/.glider_playground/missions/data/<id>/` and is registered like any
user-added file, so it goes through the normal processing queue and is never touched by the ERDDAP auto-prune.
