# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Glider Playground desktop app.

Build from the repo root:  pyinstaller packaging/glider_playground.spec --noconfirm

Produces a one-directory bundle (more reliable than one-file for the heavy
NetCDF/HDF5 stack). On macOS it is additionally wrapped into a .app.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
PKG = os.path.join(ROOT, "glider_playground")

APP_NAME = "GliderPlayground"

# --- Bundle the static web assets (HTML/JS/CSS/icons) ---------------------
datas = [(os.path.join(PKG, "static"), "glider_playground/static")]
binaries = []

# uvicorn loads its protocol/loop implementations dynamically; the FastAPI app
# is imported directly in desktop.py so its graph is followed automatically,
# but we still sweep the package to be safe.
hiddenimports = collect_submodules("uvicorn") + collect_submodules("glider_playground")

# Scientific / IO libraries that ship data files or use lazy/plugin imports
# PyInstaller's static analysis won't see on its own.
for mod in ("xarray", "netCDF4", "h5netcdf", "gsw"):
    d, b, h = collect_all(mod)
    datas += d
    binaries += b
    hiddenimports += h

# The webview backend ships native loader libraries (e.g. WebView2 on Windows).
wd, wb, wh = collect_all("webview")
datas += wd
binaries += wb
hiddenimports += wh

# --- Per-platform icon ----------------------------------------------------
icon = None
if sys.platform == "darwin":
    icon = os.path.join(PKG, "static", "app_icon.icns")
elif sys.platform == "win32":
    _ico = os.path.join(ROOT, "packaging", "app_icon.ico")
    icon = _ico if os.path.exists(_ico) else None

a = Analysis(
    [os.path.join(ROOT, "packaging", "app_entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=APP_NAME + ".app",
        icon=icon,
        bundle_identifier="org.noc.gliderplayground",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": os.environ.get("GP_VERSION", "0.0.0"),
            "CFBundleVersion": os.environ.get("GP_VERSION", "0.0.0"),
        },
    )
