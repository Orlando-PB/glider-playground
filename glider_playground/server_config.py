"""Single source of truth for server-mode / low-memory-mode/diagnostics
detection, and for backend logging setup.

`cli.py` is the only place that *decides* and *sets* IS_SERVER / LOW_MEMORY_MODE
(today: hostname sniffing for the author's Raspberry Pi, or an explicit env var
override — see SERVER_HOSTNAMES in cli.py). It must set them before the rest of
the app is imported (uvicorn.run() does that import), since the flags below are
resolved once at import time here.

Everything else should read IS_SERVER / LOW_MEMORY / DIAGNOSTICS from here
rather than re-parsing the env vars directly, so a future deployment target
(e.g. Docker, where "server mode" would come from an explicit env var rather
than a hostname guess) only has to change cli.py's detection logic in one
place.
"""

import logging
import os


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


IS_SERVER: bool = os.getenv("IS_SERVER") == "True"
LOW_MEMORY: bool = _bool_env("LOW_MEMORY_MODE")

# Off by default: the terminal only sees warnings/errors, not per-request
# timing/progress detail. Set DIAGNOSTICS_MODE=true for the verbose logs used
# when chasing performance or memory issues.
DIAGNOSTICS: bool = _bool_env("DIAGNOSTICS_MODE")


def configure_logging() -> None:
    """App-wide logging setup — the one place this is configured.

    Default: root at WARNING (third-party libraries stay quiet unless they
    have something wrong to report), our own "glider_playground" logger tree
    at INFO. DIAGNOSTICS on: our logger tree drops to DEBUG, surfacing the
    per-request timing/progress detail modules log at that level.

    copernicusmarine configures its own "copernicusmarine" logger (own
    handler, own ISO-timestamp formatter) via logging.config.dictConfig on
    import — which sets propagate=True, so every one of its lines also hits
    our root handler and prints twice, in two different formats. That's
    re-pinned here every time (dictConfig re-applies its "propagate" default
    on each import, so this must run after copernicusmarine has been
    imported — see overlay_logic._tame_copernicus_logging, called right after
    each `import copernicusmarine`).
    """
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)s - %(message)s")
    logging.getLogger("glider_playground").setLevel(logging.DEBUG if DIAGNOSTICS else logging.INFO)


def tame_copernicus_logging() -> None:
    """Stop copernicusmarine's logger from double-printing via our root handler.

    Must be called after `import copernicusmarine` (any import site) — its
    module-level dictConfig runs at that point and resets propagate=True,
    which is what causes the duplicate lines this undoes.
    """
    cm_logger = logging.getLogger("copernicusmarine")
    cm_logger.propagate = False
    cm_logger.setLevel(logging.INFO if DIAGNOSTICS else logging.WARNING)
