"""Canonical packaged location for the shared CPython/mutation worker."""

from __future__ import annotations

import os
import sys


def packaged_kernel_executable(host_executable: str = "") -> str:
    """Return the preferred packaged worker path, with legacy compatibility."""

    host = os.path.abspath(host_executable or sys.executable)
    base = os.path.dirname(host)
    name = "Variant1Kernel.exe" if sys.platform.startswith("win") else "Variant1Kernel"
    preferred = os.path.join(base, "kernel", name)
    legacy = os.path.join(base, name)
    if os.path.isfile(preferred):
        return preferred
    if os.path.isfile(legacy):
        return legacy
    return preferred


__all__ = ["packaged_kernel_executable"]
