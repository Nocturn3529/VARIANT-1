"""Canonical packaged location for the shared CPython/mutation worker."""

from __future__ import annotations

import os
import sys


def packaged_kernel_executable(host_executable: str = "") -> str:
    """Return the packaged worker path, with legacy compatibility.

    The kernel is frozen next to the backend, sharing its native libraries but
    not its module archive. Older packages kept it in its own kernel/ folder.
    """

    host = os.path.abspath(host_executable or sys.executable)
    base = os.path.dirname(host)
    name = "Variant1Kernel.exe" if sys.platform.startswith("win") else "Variant1Kernel"
    preferred = os.path.join(base, name)
    legacy = os.path.join(base, "kernel", name)
    if os.path.isfile(preferred):
        return preferred
    if os.path.isfile(legacy):
        return legacy
    return preferred


__all__ = ["packaged_kernel_executable"]
