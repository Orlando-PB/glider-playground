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


def open_browser(host):
    time.sleep(BROWSER_DELAY)
    url = f"http://{host}:{PORT}"
    print(f"Opening browser at {url} ...")
    webbrowser.open(url)


def main():
    is_server_env = os.getenv("IS_SERVER") == "True"
    current_hostname = socket.gethostname().lower()

    is_server = is_server_env or current_hostname in SERVER_HOSTNAMES

    if is_server:
        os.environ["IS_SERVER"] = "True"
        host = "0.0.0.0"
        print(f"Running in Server Mode (0.0.0.0) on {current_hostname}")
    else:
        host = "127.0.0.1"
        print("Running in Local Mode (127.0.0.1)")

    print("Starting Glider Playground...")

    threading.Thread(target=_check_for_update, daemon=True).start()

    if not is_server:
        print(f"Starting up — your browser will open shortly at http://{host}:{PORT}")
        threading.Thread(target=open_browser, args=(host,), daemon=True).start()

    # reload=False is safer for a background service
    uvicorn.run(APP_MODULE, host=host, port=PORT, log_level=LOG_LEVEL, reload=False)


if __name__ == "__main__":
    main()
