"""
VARIANT-1 path resolution — frozen-aware.

In development the backend runs as plain Python, so the app root is the parent
of the backend/ folder. When packaged with PyInstaller the modules live inside
the bundle (sys._MEIPASS), so __file__ no longer points at the project tree —
the app root must be derived from the executable's location instead.

Layout when packaged: <resources>/backend/Variant1Backend.exe, and the bundled
assets (bin/, models/, config/) sit under <resources>/. So the app root is the
grandparent of the executable. This matches the dev layout (root is the parent
of backend/), so every other module can simply do:

    from paths import APP_ROOT
"""

import os
import sys


def app_root() -> str:
    if getattr(sys, "frozen", False):
        # PyInstaller: <root>/backend/Variant1Backend.exe -> <root>
        return os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    # Dev: this file is <root>/backend/paths.py -> <root>
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_ROOT = app_root()
