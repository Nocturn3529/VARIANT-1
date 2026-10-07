"""Platform lock validation and selection."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load("validate_backend_lock", "scripts/validate_backend_lock.py")
notices = _load("collect_python_notices", "scripts/collect-python-notices.py")


def test_lock_pins_are_read_exactly_and_loose_lines_rejected(tmp_path):
    lock = tmp_path / "requirements-test.lock"
    lock.write_text(
        "# header\n\nuvicorn[standard]==0.40.0\nPyYAML==6.0.3\n", encoding="utf-8"
    )
    assert validator.read_lock(lock) == {"uvicorn": "0.40.0", "pyyaml": "6.0.3"}
    lock.write_text("httpx>=0.27\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exact pin"):
        validator.read_lock(lock)


def test_comparison_reports_missing_mismatched_and_unexpected():
    lock = {"alpha": "1.0", "beta": "2.0", "gamma": "3.0"}
    installed = {"alpha": "1.0", "beta": "2.1", "delta": "4.0", "pip": "26.0"}
    assert validator.compare(lock, installed) == {
        "missing": ["gamma"],
        "version_mismatch": [("beta", "2.0", "2.1")],
        "unexpected": ["delta"],
    }


def test_installed_versions_are_canonical():
    fake = [SimpleNamespace(metadata={"Name": "Foo_Bar"}, version="1.2")]
    assert validator.installed_versions(lambda: fake) == {"foo-bar": "1.2"}


def test_declared_dependencies_follow_platform_markers(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "pytest>=8  # tests\n"
        "uiautomation>=2 ; sys_platform == \"not-a-platform\"\n",
        encoding="utf-8",
    )
    assert validator.declared_for_this_platform(requirements) == ["pytest"]


def test_import_check_uses_the_primary_module():
    failures, _notes = validator.import_failures(["pytest"])
    assert failures == []
    failures, _notes = validator.import_failures(["variant1-not-installed"])
    assert failures and failures[0][2] == "no importable top-level module found"


@pytest.mark.parametrize(("system", "machine", "expected"), [
    ("win32", "AMD64", "requirements.lock"),
    ("linux", "x86_64", "requirements-linux-x86_64.lock"),
    ("darwin", "arm64", "requirements-macos-arm64.lock"),
    ("linux", "aarch64", "requirements.txt"),
])
def test_notices_use_this_platforms_lock_when_present(tmp_path, system, machine, expected):
    backend = tmp_path / "backend"
    backend.mkdir()
    for name in (
        "requirements.txt", "requirements.lock",
        "requirements-linux-x86_64.lock", "requirements-macos-arm64.lock",
    ):
        (backend / name).write_text("", encoding="utf-8")
    chosen = notices._dependency_inventory(tmp_path, system=system, machine=machine)
    assert chosen.name == expected


def test_notices_fall_back_to_requirements_without_a_platform_lock(tmp_path):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text("", encoding="utf-8")
    chosen = notices._dependency_inventory(tmp_path, system="darwin", machine="arm64")
    assert chosen.name == "requirements.txt"
