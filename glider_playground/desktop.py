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
from pathlib import Path

import uvicorn

from .cli import LOG_LEVEL, PORT

HOST = "127.0.0.1"
WINDOW_TITLE = "Glider Playground"
STARTUP_TIMEOUT = 30  # seconds to wait for the server before giving up

STATIC_DIR = Path(__file__).resolve().parent / "static"
ICON_ICNS = STATIC_DIR / "app_icon.icns"
ICON_PNG = STATIC_DIR / "app_icon.png"

# The web app reads ?desktop=1 to clear room for the macOS traffic-light buttons
# and to treat the title bar area as part of its top toolbar.
APP_URL = f"http://{HOST}:{PORT}/?desktop=1"


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


def _integrate_titlebar(window):
    """Merge the title bar into the content area, keeping the traffic lights.

    pywebview only offers this via ``frameless=True``, which also hides the
    close/minimise/zoom buttons. We want the unified look *with* those buttons,
    so we reach the underlying NSWindow (exposed as ``window.native``) and set
    the transparent / full-size-content style ourselves. macOS only.
    """
    try:
        import AppKit
        from PyObjCTools import AppHelper
    except ImportError:
        return  # not macOS — leave the standard frame in place

    ns_window = window.native
    if ns_window is None:
        return

    def _apply():
        ns_window.setTitlebarAppearsTransparent_(True)
        ns_window.setTitleVisibility_(1)  # NSWindowTitleHidden
        style = ns_window.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView
        ns_window.setStyleMask_(style)
        # The title bar draws its own opaque backing view on top of the content;
        # transparency alone leaves it as a solid bar over our toolbar. Clear it
        # so the web toolbar shows through. (Same view handle pywebview uses for
        # its frameless titlebar colouring.) Traffic-light buttons stay visible.
        try:
            titlebar = ns_window.contentView().superview().subviews().lastObject()
            titlebar.setBackgroundColor_(AppKit.NSColor.clearColor())
        except Exception:
            pass
        try:
            ns_window.setTitlebarSeparatorStyle_(0)  # NSTitlebarSeparatorStyleNone
        except Exception:
            pass

    # NSWindow geometry can only be touched on the main thread; the `shown`
    # event fires on a worker thread, so hop over via the AppKit run loop.
    AppHelper.callAfter(_apply)


def main():
    try:
        import webview
    except ImportError:
        raise SystemExit(
            "The desktop app needs pywebview. Install it with:\n"
            "    pip install 'glider-playground[desktop]'"
        )

    # Let blank areas of the top toolbar drag the window. pywebview drags when a
    # mousedown lands on a `.pywebview-drag-region` element; DIRECT_TARGET_ONLY
    # means only the container itself (the blank gaps) drags — clicks on the
    # buttons/dropdowns inside it still behave normally.
    webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True

    print("Starting Glider Playground (desktop)...")
    threading.Thread(target=_run_server, daemon=True).start()

    if not _wait_for_server(HOST, PORT, STARTUP_TIMEOUT):
        raise SystemExit(
            f"Server did not start within {STARTUP_TIMEOUT}s on {HOST}:{PORT}."
        )

    window = webview.create_window(
        WINDOW_TITLE,
        APP_URL,
        width=1400,
        height=900,
        min_size=(900, 600),
    )
    window.events.shown += lambda: _integrate_titlebar(window)

    # Dock/app icon. .icns is preferred; fall back to the PNG.
    icon = ICON_ICNS if ICON_ICNS.exists() else ICON_PNG
    # GP_DESKTOP_DEBUG=1 enables the WKWebView dev tools (right-click → Inspect
    # Element) for diagnosing rendering issues. Off for normal/packaged use.
    debug = os.getenv("GP_DESKTOP_DEBUG") == "1"
    # Blocks until the window is closed; the daemon server thread exits with it.
    webview.start(icon=str(icon) if icon.exists() else None, debug=debug)


if __name__ == "__main__":
    main()
