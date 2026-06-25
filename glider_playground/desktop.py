"""Desktop (windowed) launcher.

Boots the same FastAPI/uvicorn server as the web CLI on a background thread,
waits for it to accept connections, then opens a native OS window (via the
system webview) pointed at it. The web mode in ``cli.py`` is unchanged — this
is just a second front door that shows the app in its own window instead of a
browser tab.
"""

import os
import socket
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from .cli import LOG_LEVEL, PORT

HOST = "127.0.0.1"
WINDOW_TITLE = "Glider Playground"
WEBSITE_URL = "https://glider-playground.co.uk"
GITHUB_URL = "https://github.com/Orlando-PB/glider-playground"
STARTUP_TIMEOUT = 30  # seconds to wait for the server before giving up

STATIC_DIR = Path(__file__).resolve().parent / "static"
ICON_ICNS = STATIC_DIR / "app_icon.icns"
ICON_PNG = STATIC_DIR / "app_icon.png"

APP_URL = f"http://{HOST}:{PORT}/"


def _wait_for_server(host: str, port: int, timeout: int) -> bool:
    """Poll the port until the server accepts a connection (or we time out)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


def _run_server():
    # Import the app object directly (rather than the "module:app" string the
    # web CLI uses) so PyInstaller can follow the import graph when bundling.
    # Local mode only — the desktop app never binds to 0.0.0.0.
    from .app import app

    uvicorn.run(app, host=HOST, port=PORT, log_level=LOG_LEVEL, reload=False)


def _set_macos_app_name():
    """Make the macOS menu bar say "Glider Playground" instead of "Python".

    The app menu title and its About/Hide/Quit items come from the main bundle's
    ``CFBundleName``. Running unbundled (from source) that is "Python"; the
    packaged .app sets it via the PyInstaller spec. Override the in-memory value
    so the menu reads correctly in both cases before pywebview builds the menu.
    """
    try:
        from webview.platforms import cocoa
    except ImportError:
        return  # not macOS
    cocoa.info["CFBundleName"] = WINDOW_TITLE


def _build_macos_menu():
    """A small Help menu (alongside the default Edit/View menus) with links."""
    try:
        from webview.menu import Menu, MenuAction
    except ImportError:
        return []
    return [
        Menu(
            "Help",
            [
                MenuAction("Glider Playground Website", lambda: webbrowser.open(WEBSITE_URL)),
                MenuAction("View on GitHub", lambda: webbrowser.open(GITHUB_URL)),
            ],
        ),
    ]


def main():
    try:
        import webview
    except ImportError:
        raise SystemExit(
            "The desktop app needs pywebview. Install it with:\n"
            "    pip install 'glider-playground[desktop]'"
        )

    # Use the OS's standard title bar (traffic lights + native drag, double-click
    # to zoom, multi-monitor handling — all for free). We deliberately don't merge
    # the title bar into the web content: doing so means faking window dragging in
    # JavaScript, whose cross-monitor coordinate handling is unreliable.

    # macOS nicety: a sensible app/menu name (no-op off macOS).
    _set_macos_app_name()

    print("Starting Glider Playground (desktop)...")
    threading.Thread(target=_run_server, daemon=True).start()

    if not _wait_for_server(HOST, PORT, STARTUP_TIMEOUT):
        raise SystemExit(
            f"Server did not start within {STARTUP_TIMEOUT}s on {HOST}:{PORT}."
        )

    webview.create_window(
        WINDOW_TITLE,
        APP_URL,
        width=1400,
        height=900,
        min_size=(900, 600),
    )

    # Dock/app icon. .icns is preferred; fall back to the PNG.
    icon = ICON_ICNS if ICON_ICNS.exists() else ICON_PNG
    # GP_DESKTOP_DEBUG=1 enables the WKWebView dev tools (right-click → Inspect
    # Element) for diagnosing rendering issues. Off for normal/packaged use.
    debug = os.getenv("GP_DESKTOP_DEBUG") == "1"
    # Blocks until the window is closed; the daemon server thread exits with it.
    webview.start(
        icon=str(icon) if icon.exists() else None,
        debug=debug,
        menu=_build_macos_menu(),
    )


if __name__ == "__main__":
    main()
