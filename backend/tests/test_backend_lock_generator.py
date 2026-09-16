from pathlib import Path
import importlib.util
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace
import subprocess
import sys

import pytest
from packaging.requirements import Requirement

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/generate_backend_lock.py"
spec = importlib.util.spec_from_file_location("backend_lock_generator", SCRIPT)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def test_lock_follows_complete_transitive_and_extra_dependency_closure():
    installed = {
        "root": SimpleNamespace(version="1", requires=["leaf>=2", "extra-leaf; extra == 'full'", "root-cycle"]),
        "leaf": SimpleNamespace(version="2", requires=["bottom==3"]),
        "extra-leaf": SimpleNamespace(version="4", requires=[]),
        "bottom": SimpleNamespace(version="3", requires=[]),
        "root-cycle": SimpleNamespace(version="5", requires=["root"]),
    }
    result = generator.resolve_installed([Requirement("root[full]>=1")], distribution=installed.__getitem__)
    assert set(result) == set(installed)
    assert result["root"][1] == {"full"}


@pytest.mark.parametrize("requirement", ["edge-tts==0.0.0", "variant1-missing-test-package==0.0.0"])
def test_failed_lock_resolution_never_replaces_existing_lock(tmp_path, requirement):
    declared, target = tmp_path / "requirements.txt", tmp_path / "requirements.lock"
    declared.write_text(requirement + "\n", encoding="utf-8")
    target.write_bytes(b"keep previous lock\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "--requirements", str(declared), "--output", str(target)],
                            capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode != 0
    assert target.read_bytes() == b"keep previous lock\n"
