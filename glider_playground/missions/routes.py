"""HTTP surface of the missions package: /api/missions/* and the static mission pages under /missions/static."""
import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles

from ..server import server_config
from . import bundle, live_logic, mission_logic

router = APIRouter(prefix="/api/missions")
STATIC_DIR = Path(__file__).parent / "static"


def _mission(mission_id: str) -> dict:
    m = mission_logic.load(mission_id)
    if not m:
        raise HTTPException(status_code=404, detail="Unknown mission")
    return m


def _local_only() -> None:
    if server_config.IS_SERVER:
        raise HTTPException(status_code=403, detail="Not available in server mode")


@router.get("")
def api_missions():
    return {"missions": mission_logic.list_missions(), "local": not server_config.IS_SERVER,
            "live_status": live_logic.status()}


@router.get("/template")
def api_mission_template():
    _local_only()
    return FileResponse(Path(__file__).parent / "template.json", media_type="application/json")


@router.get("/guide")
def api_mission_guide():
    _local_only()
    return FileResponse(Path(__file__).parent / "README.md", media_type="text/markdown")


@router.get("/{mission_id}")
def api_mission(mission_id: str):
    return mission_logic.resolved(_mission(mission_id))


@router.get("/{mission_id}/preview")
def api_mission_preview(mission_id: str):
    return mission_logic.preview(_mission(mission_id))


@router.get("/{mission_id}/scene")
def api_mission_scene(mission_id: str, grid: int = mission_logic.BATHY_GRID):
    try:
        return mission_logic.scene(_mission(mission_id), max(50, min(grid, 1500)))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Scene unavailable: {e}")


@router.get("/{mission_id}/track/{key}")
def api_mission_track(mission_id: str, key: str, colour: str | None = None):
    return mission_logic.track(_mission(mission_id), key, colour)


@router.get("/{mission_id}/colours")
def api_mission_colours(mission_id: str):
    return {"options": mission_logic.colour_options(_mission(mission_id))}


@router.get("/{mission_id}/export")
def api_mission_export(mission_id: str, data: bool = False):
    """The mission as .json, or (?data=1) as a bundle .zip with its data files. Local installs only."""
    _local_only()
    m = _mission(mission_id)
    if not data:
        body = json.dumps(m, indent=2, ensure_ascii=False)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{m["id"]}.mission.json"'})
    tmp = Path(tempfile.mkstemp(suffix=".mission.zip")[1])
    bundle.pack(m["id"], str(tmp))
    return FileResponse(tmp, media_type="application/zip", filename=f'{m["id"]}.mission.zip',
                        background=BackgroundTask(tmp.unlink, missing_ok=True))


@router.post("/import")
async def api_mission_import(file: UploadFile = File(...)):
    """A bundle .zip or a mission .json. Local installs only; on a server drop the zip in the missions inbox
    instead (see bundle.py)."""
    _local_only()
    mission_logic.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = mission_logic.INBOX_DIR / f"_upload_{Path(file.filename or 'bundle.zip').name}.part"
    with open(tmp, "wb") as out:
        while chunk := await file.read(4 * 1024 * 1024):
            out.write(chunk)
    try:
        if (file.filename or "").lower().endswith(".json"):
            stem = Path(file.filename).name.lower().removesuffix(".json").removesuffix(".mission")
            return {"status": "success", "mission": bundle.save_json(json.loads(tmp.read_text(encoding="utf-8")), stem), "files": []}
        return {"status": "success", **bundle.import_zip(tmp)}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "message": str(e)}
    finally:
        tmp.unlink(missing_ok=True)


pages = APIRouter()
_app = None        # set in attach(): /missions URLs are answered by the app's own "/" handler (the normal shell)


@pages.get("/missions", include_in_schema=False)
@pages.get("/missions/{mission_id}", include_in_schema=False)
def mission_page(mission_id: str = ""):
    """/missions (the list) and /missions/<id> (one mission) are the shell with the mission view opened over the
    workspace by missions_shell.js, so they can be shared as plain URLs."""
    # Looked up per request: attach() runs before app.py has declared its "/" route.
    shell = next((r.endpoint for r in _app.routes if getattr(r, "path", None) == "/"), None) if _app else None
    return shell() if shell else FileResponse(STATIC_DIR / "mission_view.html")


class _Static(StaticFiles):
    """Revalidate every time (cheap 304s), so an update never leaves a stale page/script pair in the browser."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def attach(app) -> None:
    global _app
    _app = app
    app.include_router(router)
    app.include_router(pages)
    app.mount("/missions/static", _Static(directory=str(STATIC_DIR)), name="missions_static")
