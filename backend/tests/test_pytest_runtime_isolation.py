"""The default pytest process must never construct services on real VARIANT-1 data."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def test_pytest_runtime_roots_are_isolated_before_server_import():
    project = Path(__file__).resolve().parents[2]
    backend = project / "backend"
    runtime_root = Path(os.environ["VARIANT1_TEST_RUNTIME_ROOT"]).resolve()
    data_dir = Path(os.environ["VARIANT1_DATA_DIR"]).resolve()
    config_dir = Path(os.environ["VARIANT1_CONFIG"]).resolve()

    assert _contains(runtime_root, data_dir)
    assert _contains(data_dir, config_dir)
    for protected in (
        (project / "data").resolve(),
        (backend / "data").resolve(),
        (project / "config").resolve(),
    ):
        assert not _contains(protected, data_dir)
        assert not _contains(protected, config_dir)

    loaded = sys.modules.get("server")
    if loaded is not None:
        assert Path(loaded.DATA_DIR).resolve() == data_dir
        assert Path(loaded.CONFIG_DIR).resolve() == config_dir
