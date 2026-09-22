"""Live missions: BODC platforms (gliders, ALRs) reporting right now, grouped by where they are.

Nothing is stored. Each listing takes the live feed's files (server/erddap_fetch.py, which also downloads them),
clusters the READY ones by last fix and hands back ordinary mission dicts with ids "live-<first file>". A file
still downloading or processing has no position yet, so it joins its cluster once it is ready. A platform that stops
reporting stays until the feed deletes its file (erddap_fetch.PRUNE_DAYS without an update); its mission's cached
scene goes with it.
"""
import logging
import math
import time
from datetime import datetime, timezone

from ..core import cache_logic
from ..server import erddap_fetch

logger = logging.getLogger(__name__)

PREFIX = "live-"
LINK_KM = 300.0            # platforms closer than this (chained) share a mission
TTL = 60                   # seconds a built set of live missions is reused
COLOURS = ["#d6336c", "#12295c", "#0f8b8d", "#e8702a", "#7b4fb5", "#d9a514", "#2f9e44", "#5c7cfa"]

_built = {"at": 0.0, "missions": {}}


def _km(a: dict, b: dict) -> float:
    la, lb = math.radians(a["last_lat"]), math.radians(b["last_lat"])
    h = (math.sin((lb - la) / 2) ** 2
         + math.cos(la) * math.cos(lb) * math.sin(math.radians(b["last_lon"] - a["last_lon"]) / 2) ** 2)
    return 12742.0 * math.asin(min(1.0, math.sqrt(h)))


def _clusters(recs: list[dict]) -> list[list[dict]]:
    """Single linkage: two platforms share a cluster if a chain of hops under LINK_KM joins them."""
    left, out = list(recs), []
    while left:
        group, frontier = [], [left.pop()]
        while frontier:
            r = frontier.pop()
            group.append(r)
            near = [o for o in left if _km(r, o) <= LINK_KM]
            left = [o for o in left if o not in near]
            frontier += near
        out.append(sorted(group, key=lambda r: r["name"].lower()))
    return out


def _naive(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _label(name: str) -> str:
    return name[:-3].removesuffix("_R").replace("_", " ")


def _mission(group: list[dict]) -> dict | None:
    from .mission_logic import _slug
    t0, t1 = [], []
    for r in group:
        t = [x for x in ((cache_logic.get_payload(r["id"], "spatial_3d") or {}).get("time_ms") or []) if x]
        if t:
            t0.append(min(t)); t1.append(max(t))
    if not t0:
        return None
    lat = sum(r["last_lat"] for r in group) / len(group)
    lon = sum(r["last_lon"] for r in group) / len(group)
    where = f"{abs(lat):.1f}°{'N' if lat >= 0 else 'S'} {abs(lon):.1f}°{'E' if lon >= 0 else 'W'}"
    names = [_label(r["name"]) for r in group]
    title = names[0] if len(names) == 1 else f"{names[0]} + {len(names) - 1} more"
    return {
        "id": PREFIX + _slug(group[0]["name"][:-3]),
        "live": True,
        "title": f"Live: {title}",
        "summary": f"{len(group)} BODC platform{'s' if len(group) > 1 else ''} near {where}, last heard from {_naive(max(t1))[:10]}.",
        "time": {"start": _naive(min(t0)), "end": _naive(max(t1))},
        "platforms": [{"key": f"p{i}", "label": n, "file": r["name"], "colour": COLOURS[i % len(COLOURS)],
                       "show_label": True} for i, (r, n) in enumerate(zip(group, names))],
    }


def _tidy(keep: set) -> None:
    """Drop cached scenes of live missions that no longer exist."""
    from .mission_logic import SCENE_DIR
    for f in SCENE_DIR.glob(PREFIX + "*.json"):
        if not any(f.name.startswith(i + "_") for i in keep):
            f.unlink(missing_ok=True)


def missions() -> dict:
    """{id: mission} for what is live now. Touching the feed starts its scan and downloads; it never waits on them."""
    now = time.time()
    if now - _built["at"] < TTL:
        return _built["missions"]
    out = {}
    try:
        erddap_fetch.list_live()          # starts the scan, downloads and the 30-day prune
        recs = [r for r in cache_logic.list_files()
                if erddap_fetch.is_managed(r.get("path", "")) and r.get("status") == cache_logic.STATUS_READY
                and r.get("last_lat") is not None and r.get("last_lon") is not None]
        for group in _clusters(recs):
            m = _mission(group)
            if m:
                out[m["id"]] = m
    except Exception as e:  # noqa: BLE001
        logger.warning("Live missions unavailable: %s", e)
    if out:
        _tidy(set(out))
    _built.update(at=now if out else 0.0, missions=out)      # empty: the scan may just not be back yet
    return out


def status() -> dict:
    """What the live feed is still doing, for the list's status line: a scan, downloads, processing."""
    try:
        feed = erddap_fetch.list_live()
    except Exception:  # noqa: BLE001
        return {"scanning": False, "downloading": 0, "processing": 0, "error": True}
    act = [a for a in feed["active"] if not a.get("suppressed")]
    down = sum(1 for a in act if a.get("downloading") or not a.get("downloaded"))
    proc = sum(1 for a in act if a.get("downloaded") and not a.get("downloading") and a.get("status") != cache_logic.STATUS_READY)
    return {"scanning": bool(feed.get("scanning")), "downloading": down, "processing": proc, "error": bool(feed.get("scan_error"))}
