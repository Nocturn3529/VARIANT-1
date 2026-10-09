from __future__ import annotations

import os
import sys

from kernel_runtime.worker_path import packaged_kernel_executable


def _kernel_name() -> str:
    return "Variant1Kernel.exe" if sys.platform.startswith("win") else "Variant1Kernel"


def test_packaged_kernel_runs_next_to_the_backend(tmp_path):
    host = tmp_path / "Variant1Backend.exe"
    host.touch()
    preferred = tmp_path / _kernel_name()
    preferred.touch()
    legacy = tmp_path / "kernel" / _kernel_name()
    legacy.parent.mkdir()
    legacy.touch()

    assert packaged_kernel_executable(str(host)) == os.path.abspath(preferred)


def test_packaged_kernel_keeps_the_separate_folder_layout_working(tmp_path):
    host = tmp_path / "Variant1Backend.exe"
    host.touch()
    legacy = tmp_path / "kernel" / _kernel_name()
    legacy.parent.mkdir()
    legacy.touch()

    assert packaged_kernel_executable(str(host)) == os.path.abspath(legacy)


def test_missing_packaged_kernel_reports_the_preferred_location(tmp_path):
    host = tmp_path / "Variant1Backend.exe"
    expected = tmp_path / _kernel_name()

    assert packaged_kernel_executable(str(host)) == os.path.abspath(expected)
