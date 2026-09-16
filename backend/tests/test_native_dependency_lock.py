"""Native runtime dependency and removal guards."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
AGENT_ENGINE = BACKEND / "agent_engine"

REMOVED_AGENT_FRAMEWORK_PACKAGES = frozenset({
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-prebuilt",
    "langgraph-sdk",
    "langchain-core",
    "langchain-protocol",
    "langsmith",
})


def _declared_distributions(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(
            r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?\s*(?:==|>=|<=|~=|!=|>|<)",
            line.strip(),
        )
        if match:
            names.add(match.group(1).lower().replace("_", "-"))
    return names


def _locked_versions() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (BACKEND / "requirements.lock").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+)(?:\[[^]]+\])?==([^ ;]+)", line.strip())
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def test_removed_agent_framework_distributions_are_absent_from_dependency_contracts():
    requirements = _declared_distributions(BACKEND / "requirements.txt")
    lock = _declared_distributions(BACKEND / "requirements.lock")
    generator = (ROOT / "scripts" / "generate-backend-lock.ps1").read_text(
        encoding="utf-8"
    ).lower()

    assert requirements.isdisjoint(REMOVED_AGENT_FRAMEWORK_PACKAGES)
    assert lock.isdisjoint(REMOVED_AGENT_FRAMEWORK_PACKAGES)
    for package in REMOVED_AGENT_FRAMEWORK_PACKAGES:
        assert f'"{package}"' not in generator


def test_native_agent_runtime_files_and_standard_library_store_are_present():
    required_files = {
        "executor.py",
        "runner.py",
        "snapshot_store.py",
        "sqlite_snapshot_store.py",
        "state.py",
    }
    assert required_files <= {path.name for path in AGENT_ENGINE.glob("*.py")}

    store_source = (AGENT_ENGINE / "sqlite_snapshot_store.py").read_text(encoding="utf-8")
    invariant_source = (BACKEND / "core_invariants.py").read_text(encoding="utf-8")
    exports = (AGENT_ENGINE / "__init__.py").read_text(encoding="utf-8")
    assert "class SQLiteRunSnapshotStore" in store_source
    assert "import sqlite3" in store_source
    assert "import zlib" in store_source
    assert "sqlite_wal_connection" in store_source
    assert "PRAGMA journal_mode=WAL" in invariant_source
    assert "PRAGMA synchronous=FULL" in invariant_source
    assert "SQLiteRunSnapshotStore" in exports
    assert "SnapshotBoundaryCommitter" in exports


def test_agent_engine_sources_have_no_removed_framework_imports():
    forbidden_import_fragments = (
        "from langgraph",
        "import langgraph",
        "from langchain",
        "import langchain",
    )
    for path in AGENT_ENGINE.glob("*.py"):
        source = path.read_text(encoding="utf-8").lower()
        assert not any(fragment in source for fragment in forbidden_import_fragments), path


def test_astb_repl_has_no_jupyter_or_debugger_dependency_contract():
    pins = _locked_versions()
    requirements = (BACKEND / "requirements.txt").read_text(encoding="utf-8")
    generator = (ROOT / "scripts" / "generate-backend-lock.ps1").read_text(encoding="utf-8")
    forbidden = {
        "ipython", "ipykernel", "jupyter-client", "jupyter-core", "pyzmq",
        "traitlets", "comm", "debugpy", "nest-asyncio", "tornado",
        "matplotlib-inline", "jedi", "parso", "prompt-toolkit",
    }
    assert forbidden.isdisjoint(pins)
    for name in forbidden:
        assert name not in requirements.casefold()
        assert f'"{name}"' not in generator.casefold()
    assert (BACKEND / "kernel_runtime/repl_protocol.py").is_file()
    assert (BACKEND / "kernel_runtime/repl_worker.py").is_file()
