from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
import importlib.util
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "collect-python-notices.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("collect_python_notices", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


@dataclass
class FakeDist:
    name: str
    version: str = "1.0"
    requires: list[str] = field(default_factory=list)
    license_files: dict[str, str] = field(default_factory=dict)
    license_meta: str = ""
    project_urls: list[str] = field(default_factory=list)

    @property
    def metadata(self):
        class MD(dict):
            def get_all(self, key, failobj=None):
                if key == "Project-URL":
                    return list(self._urls)
                return failobj

        md = MD({"Name": self.name, "License": self.license_meta, "License-Expression": ""})
        md._urls = self.project_urls
        if self.license_meta:
            md["License"] = self.license_meta
        return md

    @property
    def files(self):
        return [types.SimpleNamespace(parts=(name,)) for name in self.license_files]

    def locate_file(self, item):
        # item may be SimpleNamespace from files
        name = item.parts[0] if hasattr(item, "parts") else str(item)
        # write temp files lazily via attached root
        path = self._root / self.name / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.license_files[name], encoding="utf-8")
        return path


def _loader_from(dists: dict[str, FakeDist], root: Path):
    for dist in dists.values():
        dist._root = root

    def loader(name: str):
        key = name.lower().replace("_", "-")
        for cand, dist in dists.items():
            if cand.lower().replace("_", "-") == key:
                return dist
        raise metadata.PackageNotFoundError(name)

    return loader


def test_strip_comment_and_marker_eval(tmp_path):
    inv = tmp_path / "requirements.txt"
    inv.write_text(
        "\n".join(
            [
                "direct>=1  # comment ok",
                'winonly>=1 ; sys_platform == "win32"',
                'posixonly>=1 ; sys_platform == "linux"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    win = mod._requirement_names(inv, environ={"sys_platform": "win32"})
    assert win == ["direct", "winonly"]
    linux = mod._requirement_names(inv, environ={"sys_platform": "linux"})
    assert linux == ["direct", "posixonly"]


def test_invalid_marker_is_hard_error(tmp_path):
    inv = tmp_path / "requirements.txt"
    inv.write_text('broken>=1 ; sys_platform === "win32"\n', encoding="utf-8")
    with pytest.raises(mod.NoticeCollectionError, match="invalid requirement|marker"):
        mod._requirement_names(inv, environ={"sys_platform": "win32"})


def test_closure_includes_indirect_and_missing_required_fails(tmp_path):
    dists = {
        "direct": FakeDist(
            "direct",
            requires=["indirect>=1"],
            license_files={"LICENSE": "direct license"},
        ),
        "indirect": FakeDist(
            "indirect",
            requires=[],
            license_files={"LICENSE": "indirect license"},
        ),
    }
    loader = _loader_from(dists, tmp_path)
    closed = mod.resolve_dependency_closure(["direct"], distribution_loader=loader)
    assert [n.lower() for n in closed] == ["direct", "indirect"]

    with pytest.raises(mod.NoticeCollectionError, match="not installed"):
        mod.resolve_dependency_closure(["missing"], distribution_loader=loader)


def test_missing_license_is_hard_error(tmp_path):
    dists = {
        "bare": FakeDist("bare", license_files={}, license_meta=""),
    }
    loader = _loader_from(dists, tmp_path)
    with pytest.raises(mod.NoticeCollectionError, match="no license"):
        mod._license_blobs(loader("bare"))


def test_collect_notices_includes_indirect_and_cpython(tmp_path):
    inv = tmp_path / "requirements.txt"
    inv.write_text("direct>=1\n", encoding="utf-8")
    cpy = tmp_path / "LICENSE.txt"
    cpy.write_text("PYTHON LICENSE", encoding="utf-8")
    dists = {
        "direct": FakeDist(
            "direct",
            requires=["indirect==1"],
            license_files={"LICENSE": "DIRECT"},
        ),
        "indirect": FakeDist(
            "indirect",
            license_meta="MIT",
            project_urls=["Homepage, https://example.invalid/indirect"],
        ),
    }
    loader = _loader_from(dists, tmp_path)
    text = mod.collect_notices(
        project_root=tmp_path,
        inventory=inv,
        distribution_loader=loader,
        cpython_license=cpy,
    )
    assert "DIRECT" in text
    assert "MIT" in text
    assert "indirect" in text.lower()
    assert "PYTHON LICENSE" in text
