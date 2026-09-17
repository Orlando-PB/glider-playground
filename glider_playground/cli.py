"""`glider-playground` command — the front door.

Picks server vs local mode (IS_SERVER env var, or a known Pi hostname) and
sets IS_SERVER *before* the app is imported, because
server_config resolves them once at import. Then runs uvicorn on --port
(default 8420): 127.0.0.1 locally, 0.0.0.0 in server mode. Prints upgrade
steps if PyPI has a newer release; locally, opens a browser tab unless
--no-browser.
"""

import argparse
import os
import socket
import uvicorn
import threading
import webbrowser
import time

# --- Configurable Variables ---
PORT = 8420
APP_MODULE = "glider_playground.app:app"
BROWSER_DELAY = 15
LOG_LEVEL = "warning"
SERVER_HOSTNAMES = ["raspberrypi", "server", "server.local"]
# ------------------------------

ENV_VAR_HELP = """
environment variables:
  IS_SERVER          "True" to force server mode (binds 0.0.0.0, throttles
                      background processing, enables server-only plugins).
                      Auto-detected on hostnames: %s
  GP_DATA_DIR         directory scanned/used for NetCDF (.nc) files.
  GP_PLUGINS_DIR      directory of server-only plugin .py files
                      (default: ~/.glider_playground/plugins), IS_SERVER only.
  DIAGNOSTICS_MODE    "true" for verbose backend DEBUG logging.
""" % ", ".join(SERVER_HOSTNAMES)


def _check_for_update():
    """Print the same upgrade steps the in-app (Jelly) notice shows, tailored to git vs pip installs."""
    try:
        from .server import update_logic
        info = update_logic.check()
        if info.get("outdated"):
            steps = "".join(f"\n    {s}" for s in info.get("steps", []))
            print(f"\n  Update available: {info['current']} → {info['latest']}\n  To update, run:{steps}\n")
    except Exception:
        pass


def open_browser(host, port=PORT):
    # Open as soon as the server accepts connections; BROWSER_DELAY is the give-up-and-open-anyway cap.
    deadline = time.time() + BROWSER_DELAY
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    url = f"http://{host}:{port}"
    print(f"Opening browser at {url} ...")
    webbrowser.open(url)


def _parse_args():
    parser = argparse.ArgumentParser(
        prog="glider-playground",
        description="Local-first browser explorer for ocean glider NetCDF (OG1) data.",
        epilog=ENV_VAR_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--port", type=int, default=PORT,
        help=f"port to serve on (default: {PORT})",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="don't automatically open a browser tab on startup",
    )
    try:
        version = __import__("importlib.metadata", fromlist=["version"]).version(
            "glider-playground"
        )
    except Exception:
        version = "unknown"
    parser.add_argument(
        "--version", action="version", version=f"glider-playground {version}",
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    is_server_env = os.getenv("IS_SERVER") == "True"
    current_hostname = socket.gethostname().lower()

    is_server = is_server_env or current_hostname in SERVER_HOSTNAMES

    if is_server:
        os.environ["IS_SERVER"] = "True"
        host = "0.0.0.0"
        print(f"Running in Server Mode (0.0.0.0) on {current_hostname}")
    else:
        host = "127.0.0.1"
        print("Running in Local Mode (localhost)")

    print("Starting Glider Playground...")

    threading.Thread(target=_check_for_update, daemon=True).start()

    if not is_server and not args.no_browser:
        shown = "localhost" if host == "127.0.0.1" else host   # still binds 127.0.0.1; just friendlier to read
        print(f"Starting up — your browser will open shortly at http://{shown}:{args.port}")
        threading.Thread(target=open_browser, args=(shown, args.port), daemon=True).start()

    # reload=False is safer for a background service
    uvicorn.run(APP_MODULE, host=host, port=args.port, log_level=LOG_LEVEL, reload=False)


if __name__ == "__main__":
    main()
