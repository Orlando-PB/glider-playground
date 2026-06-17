"""PyInstaller entry point for the desktop build.

Kept as a thin wrapper so the frozen executable has a single, import-light
launch script. All real logic lives in ``glider_playground.desktop``.
"""

import multiprocessing

from glider_playground.desktop import main

if __name__ == "__main__":
    # Required so a frozen (PyInstaller) build doesn't re-spawn the whole app
    # if any dependency uses multiprocessing.
    multiprocessing.freeze_support()
    main()
