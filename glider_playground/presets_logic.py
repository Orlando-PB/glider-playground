"""Loader for static/plot_presets.json — the single source of truth for plot
presets, dashboard views and colour palettes. The backend uses it for the
default-plot prewarm (cache_logic); pages get it as a blocking script from
/api/plot_presets.js so it's available synchronously at first paint."""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "static" / "plot_presets.json"

_cache = {"mtime": None, "cfg": None}


def load() -> dict:
    """Parsed config, re-read only when the file changes. A broken file keeps
    serving the last good copy (or an empty config) rather than taking the app down."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
        if _cache["cfg"] is None or mtime != _cache["mtime"]:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.pop("_help", None)
            _cache.update(mtime=mtime, cfg=cfg)
    except Exception as e:
        print(f"[presets] could not load {CONFIG_PATH.name}: {e}")
        if _cache["cfg"] is None:
            _cache["cfg"] = {"axes": {}, "presets": {}, "views": {}, "palettes": {}}
    return _cache["cfg"]


def _candidates(cfg: dict, spec) -> list:
    """An x/y/c entry is either a list of names or the name of a shared 'axes' list."""
    if isinstance(spec, str):
        return list(cfg.get("axes", {}).get(spec, [spec]))
    return list(spec or [])


def prewarm_candidates() -> list:
    """[(x_candidates, y_candidates, c_candidates), ...] for every preset with prewarm: true."""
    cfg = load()
    return [
        (_candidates(cfg, p.get("x")), _candidates(cfg, p.get("y")), _candidates(cfg, p.get("c")))
        for p in cfg.get("presets", {}).values()
        if p.get("prewarm")
    ]


def as_script() -> str:
    return "window.GP_PLOT_PRESETS = " + json.dumps(load(), separators=(",", ":")) + ";"
