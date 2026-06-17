"""Generate packaging/app_icon.ico from the PNG logo (run on the Windows job).

Windows executables need a multi-resolution .ico; we keep only the .icns/.png
in the repo and derive the .ico at build time with Pillow.
"""

import os

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, os.pardir, "glider_playground", "static", "app_icon.png")
OUT = os.path.join(HERE, "app_icon.ico")

img = Image.open(SRC).convert("RGBA")
img.save(
    OUT,
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("Wrote", OUT)
