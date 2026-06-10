"""Update check.

Compares the installed `glider-playground` version against the latest on PyPI
and, when out of date, works out the most relevant upgrade instructions for
*this* install: a git checkout gets `git pull`, a pip install gets
`pip install --upgrade`, and if we can see the active virtualenv/conda env we
prefix the command with how to activate it. The PyPI lookup is cached so a busy
page doesn't hammer the index.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

PYPI_JSON_URL = "https://pypi.org/pypi/glider-playground/json"
PACKAGE_NAME = "glider-playground"
CACHE_TTL = 3600  # seconds — re-check PyPI at most once an hour per process
HTTP_TIMEOUT = 5

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}


def _installed_version() -> str | None:
    try:
        import importlib.metadata
        return importlib.metadata.version(PACKAGE_NAME)
    except Exception:
        return None


def _latest_version() -> str | None:
    try:
        with urllib.request.urlopen(PYPI_JSON_URL, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read())["info"]["version"]
    except Exception:
        return None


def _version_key(v: str) -> tuple:
    """Loose PEP 440-ish key: leading integer of each dot-part. Good enough to
    order simple releases like 0.2.14 without pulling in `packaging`."""
    out = []
    for part in str(v).split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _is_outdated(current: str | None, latest: str | None) -> bool:
    if not current or not latest:
        return False
    try:
        return _version_key(latest) > _version_key(current)
    except Exception:
        return current != latest


def _detect_install() -> tuple[str, str | None]:
    """Return ("git" | "pip" | "unknown", repo_dir_or_None).

    A `.git` directory beside the package (the repo root of a clone or an
    editable `pip install -e .`) means git; living under site/dist-packages
    means a normal pip install; anything else is unknown.
    """
    pkg_dir = Path(__file__).resolve().parent       # .../glider_playground
    repo_root = pkg_dir.parent                       # repo root for a checkout
    try:
        if (repo_root / ".git").exists():
            return "git", str(repo_root)
    except Exception:
        pass
    low = str(pkg_dir).lower()
    if "site-packages" in low or "dist-packages" in low:
        return "pip", None
    return "unknown", None


def _detect_env() -> dict:
    """Best-effort active-environment detection so we can tell the user to
    activate it before upgrading. Returns kind/name/activate (any may be None)."""
    conda = os.getenv("CONDA_DEFAULT_ENV")
    if conda:
        return {"kind": "conda", "name": conda, "activate": f"conda activate {conda}"}

    # A venv/virtualenv has sys.prefix != sys.base_prefix.
    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    if sys.prefix != base_prefix:
        name = os.path.basename(sys.prefix.rstrip("/\\")) or "venv"
        if os.name == "nt":
            activate = str(Path(sys.prefix) / "Scripts" / "activate")
        else:
            activate = f"source {Path(sys.prefix) / 'bin' / 'activate'}"
        return {"kind": "venv", "name": name, "activate": activate}

    return {"kind": None, "name": None, "activate": None}


def _build_steps(method: str, repo_dir: str | None, env: dict) -> list[str]:
    """Ordered shell commands to get from "out of date" to "up to date"."""
    steps: list[str] = []
    if env.get("activate"):
        steps.append(env["activate"])

    if method == "git":
        # `git -C <dir>` works regardless of the user's cwd.
        steps.append(f'git -C "{repo_dir}" pull' if repo_dir else "git pull")
        # An editable install just needs the pull; a plain checkout that was
        # `pip install .`-ed benefits from reinstalling. Mention it lightly.
        steps.append(f'pip install -e "{repo_dir}"' if repo_dir else "pip install -e .")
    else:
        # pip and unknown both upgrade from PyPI.
        steps.append(f"pip install --upgrade {PACKAGE_NAME}")
    return steps


def _compute() -> dict:
    current = _installed_version()
    latest = _latest_version()
    outdated = _is_outdated(current, latest)

    info = {
        "current": current,
        "latest": latest,
        "outdated": outdated,
        "method": "unknown",
        "env": {"kind": None, "name": None, "activate": None},
        "steps": [],
    }
    if not outdated:
        return info

    method, repo_dir = _detect_install()
    env = _detect_env()
    info["method"] = method
    info["env"] = env
    info["steps"] = _build_steps(method, repo_dir, env)
    return info


def check(force: bool = False) -> dict:
    """Cached update check. Safe to call on every page load."""
    now = time.time()
    with _lock:
        fresh = (now - _cache["at"]) < CACHE_TTL
        if not force and fresh and _cache["data"] is not None:
            return _cache["data"]
    try:
        data = _compute()
    except Exception:
        data = {"current": None, "latest": None, "outdated": False,
                "method": "unknown", "env": {}, "steps": []}
    with _lock:
        _cache.update(at=time.time(), data=data)
    return data
