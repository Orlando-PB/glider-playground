"""Research-ship positions (experimental, self-contained).

Everything ship-related lives here + ``static/ships_layer.js``; the rest of
the app only touches it through one ``/api/ships`` route in ``app.py`` and
three one-line hooks in ``map_view.html``. Delete those and this file and the
playground is back to gliders/floats-only.

Two free, key-less upstream sources (no AIS provider needed):

* **NOC** — the "Where are our ships right now?" block on
  ``noc.ac.uk/our-work/ships-and-expeditions`` embeds a week of daily
  position reports for RRS Discovery and RRS James Cook as a GeoJSON blob in a
  ``data-geojson`` attribute (hand-typed bridge reports: DTG + zone, position,
  expedition, status, weather). We scrape that attribute. If NOC restyles the
  page this breaks gracefully (ships just disappear from the layer).
* **BAS** — ``nerc-bas.ac.uk/icd/data/ship-pos/sda.pos``: hourly positions of
  RRS Sir David Attenborough derived from its automatic weather-station met
  reports. Plain text ``YY MM DD HH lat lon``, ~1000 rows (≈6 weeks).

Only the latest fix per ship is returned: both feeds are coarse (BAS rounds
to 0.1°, NOC reports are hand-typed to ~2 dp), so a track drawn from them is
misleading. Both are cached in-process for ``TTL``; a failed refresh keeps
serving the last good result. Times are returned as naive-UTC ISO strings with an
explicit ``Z`` (same convention as ``argo_logic``).
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

NOC_URL = "https://www.noc.ac.uk/our-work/ships-and-expeditions"
BAS_URLS = (
    "https://www.nerc-bas.ac.uk/icd/data/ship-pos/sda.pos",
    "http://www.nerc-bas.ac.uk/icd/data/ship-pos/sda.pos",
)
TTL = 30 * 60           # NOC reports are daily, BAS hourly — 30 min is plenty
HTTP_TIMEOUT = 20
MAX_AGE_DAYS = 14       # ignore BAS rows older than this (file spans ~6 weeks)

_lock = threading.Lock()
_cache: Optional[dict] = None      # {"built": epoch, "ships": [...]}
_refreshing = False

# Static identity for the marker/card; positions come from the feeds.
# ``icon`` names ``static/icons/<icon>-mapicon.png`` (vendored MARS icons).
SHIPS = {
    "discovery": {
        "name": "RRS Discovery", "operator": "NOC / NERC", "source": "NOC daily position report",
        "link": "https://www.noc.ac.uk/our-work/ships-and-expeditions",
        "mmsi": "235091165", "imo": "9588029", "icon": "rrs-discovery",
    },
    "james_cook": {
        "name": "RRS James Cook", "operator": "NOC / NERC", "source": "NOC daily position report",
        "link": "https://www.noc.ac.uk/our-work/ships-and-expeditions",
        "mmsi": "235010700", "imo": "9338242", "icon": "rrs-james-cook",
    },
    "sda": {
        "name": "RRS Sir David Attenborough", "operator": "BAS", "source": "BAS met-report position (hourly)",
        "link": "https://legacy.bas.ac.uk/met/services/ship-pos.html",
        "mmsi": "740405000", "imo": "9798222", "icon": "ship",
    },
}
_NOC_NAME_TO_ID = {"rrs discovery": "discovery", "rrs james cook": "james_cook"}

# Bridge reports quote the zone loosely: "BST", "GMT+1", " UTC+1", "GMT", "UTC".
_ZONE_OFFSETS = {"bst": 1, "gmt": 0, "utc": 0, "z": 0, "": 0}


def _zone_offset_hours(zone: str) -> int:
    z = (zone or "").strip().lower().replace(" ", "")
    if z in _ZONE_OFFSETS:
        return _ZONE_OFFSETS[z]
    m = re.fullmatch(r"(?:gmt|utc)?([+-]\d{1,2})(?::?(\d\d))?", z)
    if m:
        return int(m.group(1))
    return 0


def _parse_dtg(dtg: str, zone: str) -> Optional[str]:
    """'DDMMYY HHMM' + zone → 'YYYY-MM-DDTHH:MM:00Z' (UTC)."""
    m = re.search(r"(\d{2})(\d{2})(\d{2})\s+(\d{2})(\d{2})", dtg or "")
    if not m:
        return None
    d, mo, y, hh, mm = (int(g) for g in m.groups())
    try:
        t = datetime(2000 + y, mo, d, hh, mm) - timedelta(hours=_zone_offset_hours(zone))
    except ValueError:
        return None
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


# ---------- NOC ----------

def _fetch_noc() -> list[dict]:
    r = requests.get(NOC_URL, timeout=HTTP_TIMEOUT, headers={"User-Agent": "glider-playground"})
    r.raise_for_status()
    m = re.search(r'data-geojson="([^"]*)"', r.text)
    if not m:
        raise RuntimeError("NOC page has no data-geojson block")
    blob = json.loads(html.unescape(m.group(1)))
    out = []
    for coll in blob.values():
        sid = _NOC_NAME_TO_ID.get(_clean(coll.get("name")).lower())
        if not sid:
            continue
        fixes = []
        for f in coll.get("features") or []:
            geom = f.get("geometry") or {}
            if geom.get("type") != "Point":
                continue   # the LineString is just the same points joined up
            try:
                lon, lat = float(geom["coordinates"][0]), float(geom["coordinates"][1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            p = f.get("properties") or {}
            t = _parse_dtg(p.get("dtg"), p.get("zone")) or _clean(p.get("created"))[:19].replace(" ", "T") + "Z"
            fixes.append({
                "lat": round(lat, 4), "lon": round(lon, 4), "time": t,
                "expedition": _clean(p.get("expedition")), "status": _clean(p.get("status")),
                "intentions": _clean(p.get("intentions")), "wx": _clean(p.get("wx")),
                "speed": _clean(p.get("speed")),
            })
        if not fixes:
            continue
        fixes.sort(key=lambda x: x["time"])
        last = fixes[-1]
        out.append({
            **SHIPS[sid], "id": sid,
            "lat": last["lat"], "lon": last["lon"], "time": last["time"],
            "expedition": last["expedition"], "status": last["status"],
            "intentions": last["intentions"], "wx": last["wx"], "speed": last["speed"],
            "precision": "bridge report, ~2 dp (a few km)",
        })
    return out


# ---------- BAS ----------

def _fetch_bas() -> list[dict]:
    text = None
    err: Optional[Exception] = None
    for url in BAS_URLS:
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            text = r.text
            break
        except Exception as e:  # noqa: BLE001
            err = e
    if text is None:
        raise RuntimeError(f"BAS position file unavailable: {err}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).replace(tzinfo=None)
    last = None
    for line in text.splitlines():
        p = line.split()
        if len(p) < 6:
            continue
        try:
            y, mo, d, hh = (int(x) for x in p[:4])
            lat, lon = float(p[4]), float(p[5])
            t = datetime(2000 + y, mo, d, hh)
        except ValueError:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or t < cutoff:
            continue
        if last is None or t >= last[0]:
            last = (t, lat, lon)
    if last is None:
        raise RuntimeError("BAS position file had no recent rows")
    return [{
        **SHIPS["sda"], "id": "sda",
        "lat": last[1], "lon": last[2], "time": last[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expedition": "", "status": "", "intentions": "", "wx": "", "speed": "",
        "precision": "met report, 0.1° (~5–11 km)",
    }]


# ---------- cache ----------

def _refresh() -> dict:
    ships: list[dict] = []
    errors: list[str] = []
    for name, fn in (("NOC", _fetch_noc), ("BAS", _fetch_bas)):
        try:
            ships.extend(fn())
        except Exception as e:  # noqa: BLE001
            log.warning("ships: %s fetch failed: %s", name, e)
            errors.append(f"{name}: {e}")
    return {"built": time.time(), "ships": ships, "errors": errors}


def _refresh_worker():
    global _cache, _refreshing
    try:
        data = _refresh()
        with _lock:
            if data["ships"] or _cache is None:
                _cache = data
            else:   # total failure: keep the old positions, surface the errors
                _cache = {**_cache, "built": time.time(), "errors": data["errors"]}
    finally:
        with _lock:
            _refreshing = False


def list_ships() -> dict:
    """Latest position for each known research ship.

    First call blocks on the fetch (a few seconds); later calls serve the
    cache and refresh it in the background once it's older than ``TTL``.
    """
    global _cache, _refreshing
    with _lock:
        data = _cache
        stale = data is None or (time.time() - data["built"]) > TTL
        if stale and not _refreshing:
            _refreshing = True
            if data is None:
                pass   # fetch synchronously below
            else:
                threading.Thread(target=_refresh_worker, name="ships-refresh", daemon=True).start()
    if data is None:
        try:
            data = _refresh()
            with _lock:
                _cache = data
        finally:
            with _lock:
                _refreshing = False
    return {
        "status": "ready",
        "built": datetime.fromtimestamp(data["built"], tz=timezone.utc).isoformat(timespec="seconds"),
        "errors": data.get("errors") or [],
        "ships": data["ships"],
    }
