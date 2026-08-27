"""Waypoint storage — manually curated target points (e.g. planned stations)
associated with a glider, shown as pins on the globe.

There is no canonical glider id anywhere else in this app (identity is
per-file, and file ids/paths change as live data updates), so a waypoint is
tagged with a free-text `glider` string and matched against filenames by a
case-insensitive substring check — good enough for "which track is this a
target for" without inventing a glider registry.

Persisted the same way live_logic.py tracks managed files: a small JSON file
under CACHE_ROOT, loaded/saved whole under a lock. No versioning needed since
waypoints aren't derived from file content.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

from . import cache_logic
from . import server_config

WAYPOINTS_FILE = cache_logic.CACHE_ROOT / "waypoints.json"

_lock = threading.RLock()

# One-time bootstrap for the public server: written the first time
# waypoints.json doesn't exist yet, in the exact schema add_waypoint()
# produces, so the entries are indistinguishable from (and editable/removable
# as) admin-added waypoints from then on. Gated to IS_SERVER so pip/desktop
# installs don't get someone else's stations seeded into their local store.
# Remove this once the admin plugin has been used to manage these for real.
_SEED_WAYPOINTS = [
    {
        "glider": "Stella",
        "tag": "Station 11",
        "lat": 56.78,
        "lon": -47.64,
        "notes": "unit_436 (deployment 713): turn off science at 761 Ah used, then head here.",
    },
    {
        "glider": "Stella",
        "tag": "Station 12",
        "lat": 55.348,
        "lon": -46.599,
        "notes": "unit_436 (deployment 713): head here from Station 11.",
    },
]


def _seed_if_empty() -> None:
    """Write the seed list straight to disk (bypassing add_waypoint, which
    would call back into _load and recurse into this same check)."""
    if not server_config.IS_SERVER or WAYPOINTS_FILE.exists():
        return
    now = time.time()
    seeded = [
        {
            "id": uuid.uuid4().hex,
            "glider": w["glider"],
            "tag": w["tag"],
            "lat": w["lat"],
            "lon": w["lon"],
            "notes": w.get("notes", ""),
            "created_at": now,
        }
        for w in _SEED_WAYPOINTS
    ]
    _save(seeded)


def _load() -> list:
    if not WAYPOINTS_FILE.exists():
        with _lock:
            _seed_if_empty()
    if not WAYPOINTS_FILE.exists():
        return []
    try:
        data = json.loads(WAYPOINTS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(waypoints: list) -> None:
    cache_logic.CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    WAYPOINTS_FILE.write_text(json.dumps(waypoints, indent=2))


def list_waypoints(glider: str | None = None) -> list:
    waypoints = _load()
    if glider:
        needle = glider.strip().lower()
        waypoints = [w for w in waypoints if needle in (w.get("glider") or "").lower()]
    return waypoints


def add_waypoint(glider: str, lat: float, lon: float, tag: str, notes: str = "") -> dict:
    glider = (glider or "").strip()
    tag = (tag or "").strip()
    if not glider:
        raise ValueError("glider is required")
    if not tag:
        raise ValueError("tag is required")
    lat = float(lat)
    lon = float(lon)
    if not (-90 <= lat <= 90):
        raise ValueError("lat must be between -90 and 90")
    if not (-180 <= lon <= 180):
        raise ValueError("lon must be between -180 and 180")

    waypoint = {
        "id": uuid.uuid4().hex,
        "glider": glider,
        "tag": tag,
        "lat": lat,
        "lon": lon,
        "notes": (notes or "").strip(),
        "created_at": time.time(),
    }
    with _lock:
        waypoints = _load()
        waypoints.append(waypoint)
        _save(waypoints)
    return waypoint


def remove_waypoint(waypoint_id: str) -> bool:
    with _lock:
        waypoints = _load()
        remaining = [w for w in waypoints if w.get("id") != waypoint_id]
        if len(remaining) == len(waypoints):
            return False
        _save(remaining)
        return True
