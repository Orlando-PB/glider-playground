"""Mission bundles: one .zip holding mission.json + the full data files, to share a mission or move it to a server.

    python -m glider_playground.missions pack biocarbon -o biocarbon.zip     # mission + every .nc it references
    python -m glider_playground.missions import biocarbon.zip                # unpack + register the files

On a server, copy the zip into <cache root>/missions/inbox/ (scp / the admin file manager); it is imported the
next time the mission list is requested. Files are stored uncompressed in the zip (NetCDF is already compressed).
"""
import json
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path

from ..core import cache_logic
from . import mission_logic as ml

logger = logging.getLogger(__name__)


def pack(mission_id: str, out: str | None = None) -> Path:
    m = ml.load(mission_id)
    if not m:
        raise SystemExit(f"No mission '{mission_id}'. Known: {', '.join(ml._mission_files()) or 'none'}")
    index = ml._file_index()
    dest = Path(out or f"{m['id']}.mission.zip")
    missing, manifest = [], {}
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        packed = json.loads(json.dumps(m))
        for p in packed.get("platforms", []):
            rec = ml._resolve(p, index)
            if not rec:
                missing.append(p.get("key"))
                continue
            p["file"] = rec["name"]                    # pin the bundle to the file actually packed
            if rec["name"] not in manifest:
                z.write(rec["path"], f"data/{rec['name']}")
                manifest[rec["name"]] = {"mtime": os.path.getmtime(rec["path"]), "size": os.path.getsize(rec["path"])}
        z.writestr("manifest.json", json.dumps(manifest, indent=2))      # exact mtimes: import keeps the newest copy
        z.writestr("mission.json", json.dumps(packed, indent=2, ensure_ascii=False))
    if missing:
        logger.warning("Packed without data for: %s", ", ".join(missing))
    return dest


def save_json(m: dict, fallback_id: str = "mission") -> str:
    if not isinstance(m, dict) or not isinstance(m.get("platforms", []), list):
        raise ValueError("Not a mission: expected a JSON object (see the template)")
    mid = ml._slug(m.get("id") or fallback_id)
    ml.MISSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (ml.MISSIONS_DIR / f"{mid}.json").write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    return mid


def import_zip(path: str | Path) -> dict:
    """Unpack a bundle. A data file that is already registered under the same name is only replaced by a newer
    one: the older copy stays on disk untouched, and missions resolve to the newest (ml._file_index)."""
    path = Path(path)
    files = []
    with zipfile.ZipFile(path) as z:
        m = json.loads(z.read("mission.json").decode("utf-8"))
        mid = ml._slug(m.get("id") or path.stem.replace(".mission", ""))
        manifest = json.loads(z.read("manifest.json")) if "manifest.json" in z.namelist() else {}
        have = ml._file_index()
        data_dir = ml.DATA_DIR / mid
        for info in z.infolist():
            name = Path(info.filename).name                  # flatten: never trust paths inside a zip
            if info.is_dir() or not info.filename.startswith("data/") or not name.lower().endswith(".nc"):
                continue
            mtime = (manifest.get(name) or {}).get("mtime") or time.mktime(info.date_time + (0, 0, -1))
            old = have.get(name.lower())
            if old and old.get("exists") and old.get("mtime", 0) >= mtime - 2:      # zip timestamps are 2 s coarse
                files.append({"name": name, "action": "kept existing (same or newer)"})
                continue
            data_dir.mkdir(parents=True, exist_ok=True)
            target = data_dir / name
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            os.utime(target, (mtime, mtime))
            cache_logic.register_path(str(target))
            files.append({"name": name, "action": "replaced by newer copy" if old else "added"})
    return {"mission": save_json(m, mid), "files": files}


def import_inbox() -> None:
    if not ml.INBOX_DIR.is_dir():
        return
    for z in sorted(ml.INBOX_DIR.glob("*.zip")):
        try:
            mid = import_zip(z)["mission"]
            z.rename(z.with_suffix(".zip.imported"))
            logger.info("Imported mission bundle %s -> %s", z.name, mid)
        except Exception:  # noqa: BLE001
            logger.exception("Mission bundle %s failed to import", z.name)
            z.rename(z.with_suffix(".zip.failed"))
