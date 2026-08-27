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
BROWSER_DELAY = 5
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
  LOW_MEMORY_MODE     "true" to reduce in-RAM preload / point budgets.
  DIAGNOSTICS_MODE    "true" for verbose backend DEBUG logging.
""" % ", ".join(SERVER_HOSTNAMES)


def _check_for_update():
    """Print a nudge if a newer version is available on PyPI."""
    try:
        import importlib.metadata
        import urllib.request, json
        current = importlib.metadata.version("glider-playground")
        with urllib.request.urlopen(
            "https://pypi.org/pypi/glider-playground/json", timeout=5
        ) as r:
            latest = json.loads(r.read())["info"]["version"]
        if latest != current:
            print(
                f"\n  Update available: {current} → {latest}"
                f"\n  Run: pip install --upgrade glider-playground\n"
            )
    except Exception:
        pass


def open_browser(host, port=PORT):
    time.sleep(BROWSER_DELAY)
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
        os.environ["LOW_MEMORY_MODE"] = "true"
        host = "0.0.0.0"
        print(f"Running in Server Mode (0.0.0.0) on {current_hostname}")
    else:
        host = "127.0.0.1"
        print("Running in Local Mode (127.0.0.1)")

    print("Starting Glider Playground...")

    threading.Thread(target=_check_for_update, daemon=True).start()

    if not is_server and not args.no_browser:
        print(f"Starting up — your browser will open shortly at http://{host}:{args.port}")
        threading.Thread(target=open_browser, args=(host, args.port), daemon=True).start()

    # reload=False is safer for a background service
    uvicorn.run(APP_MODULE, host=host, port=args.port, log_level=LOG_LEVEL, reload=False)


if __name__ == "__main__":
    main()
