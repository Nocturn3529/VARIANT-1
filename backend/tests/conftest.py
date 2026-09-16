"""Shared pytest configuration for VARIANT-1 backend tests."""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

# Pytest creates tmp_path directories with mode 0o700. Some managed Windows
# environments then deny the creating process access, so relax only that mode.
if os.name == "nt":
    try:
        import _pytest.pathlib as _pytest_pathlib

        _orig_mkdir = _pytest_pathlib.os.mkdir

        def _mkdir_readable(path, mode=0o777, *args, **kwargs):
            if mode == 0o700:
                mode = 0o755
            return _orig_mkdir(path, mode, *args, **kwargs)

        _pytest_pathlib.os.mkdir = _mkdir_readable
    except Exception:
        pass

# Runtime imports backend modules as top-level modules.
_BACKEND = Path(__file__).resolve().parent.parent
_PROJECT = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# Construct an isolated VARIANT-1 data/config root before pytest imports a module
# that imports server.py. The Node runner and direct pytest invocations both
# use a system-temp root outside source-control/indexer reach.
_supplied_test_root = str(os.environ.get("VARIANT1_TEST_RUNTIME_ROOT") or "").strip()
_owns_test_root = not bool(_supplied_test_root)
_TEST_RUNTIME_ROOT = Path(
    _supplied_test_root
    or tempfile.mkdtemp(prefix="variant1-pytest-runtime-")
).resolve()
_TEST_DATA_DIR = (_TEST_RUNTIME_ROOT / "variant1-data").resolve()
_TEST_CONFIG_DIR = (_TEST_DATA_DIR / "config").resolve()
_TEST_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

_PATH_OVERRIDE_VARS = (
    "VARIANT1_AGENT_SNAPSHOT_DB",
    "VARIANT1_AGENT_SNAPSHOT_DIR",
    "VARIANT1_AUTOMATIONS",
    "VARIANT1_BROWSER_FABRIC_DB",
    "VARIANT1_BROWSER_PROFILE_ROOT",
    "VARIANT1_CODING_DB",
    "VARIANT1_CODING_WORKTREE_ROOT",
    "VARIANT1_CONVERSATION_DB",
    "VARIANT1_DESKTOP_FABRIC_DB",
    "VARIANT1_EXECUTION_DB",
    "VARIANT1_MESSAGING_CONFIG",
    "VARIANT1_PATCH_JOURNAL_DIR",
    "VARIANT1_TOOLS_CONFIG",
    "VARIANT1_TRACE_PATH",
    "VARIANT1_WORK_DB",
    "VARIANT1_WORKSPACE_DB",
)
for _name in _PATH_OVERRIDE_VARS:
    os.environ.pop(_name, None)
os.environ["VARIANT1_TEST_RUNTIME_ROOT"] = str(_TEST_RUNTIME_ROOT)
os.environ["VARIANT1_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["VARIANT1_CONFIG"] = str(_TEST_CONFIG_DIR)
os.environ["VARIANT1_LLM_CONFIG"] = str(
    (_PROJECT / "config" / "llm_config.default.json").resolve()
)


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


for _protected in (
    (_PROJECT / "data").resolve(),
    (_BACKEND / "data").resolve(),
    (_PROJECT / "config").resolve(),
):
    if _contains(_protected, _TEST_DATA_DIR) or _contains(
        _protected, _TEST_CONFIG_DIR
    ):
        raise RuntimeError(
            "pytest VARIANT-1 runtime root overlaps development state: "
            f"{_protected}"
        )


def pytest_sessionfinish(session, exitstatus):
    del session, exitstatus
    if _owns_test_root:
        shutil.rmtree(_TEST_RUNTIME_ROOT, ignore_errors=True)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(*parts: str) -> str:
    """Read a UTF-8 fixture under tests/fixtures/."""
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_desktop_control_state():
    """Bind isolated driver scratch; Desktop Fabric owns durable test state."""
    import desktop_control as dc
    from desktop.session import DesktopSessionState, bind_desktop_session

    state = DesktopSessionState(session_id="desktop_test_driver")
    with bind_desktop_session(state):
        yield


def _loaded_app():
    server = sys.modules.get("server")
    return getattr(server, "APP", None) if server is not None else None


@pytest.fixture(autouse=True)
def _isolate_conversation_sessions(tmp_path, tmp_path_factory):
    """Redirect the runtime-owned chat/SessionRuntime graph for a test."""
    app = _loaded_app()
    if app is None or getattr(app, "runtime", None) is None:
        yield
        return
    from tests.support.conversation_sessions import open_sessions

    runtime = app.require_runtime()
    previous = runtime.sessions
    previous_registry = runtime.session_runtimes
    catalog = runtime.catalog
    kernel = runtime.kernel
    previous_catalog_registry = (
        getattr(catalog, "runtime_registry", None) if catalog is not None else None
    )
    previous_mutation_registry = (
        getattr(getattr(catalog, "mutation", None), "runtime_registry", None)
        if catalog is not None else None
    )
    previous_kernel_registry = (
        getattr(kernel, "registry", None) if kernel is not None else None
    )
    # Keep the AppHost's test chat database outside paths a test may enumerate.
    conversation_root = tmp_path_factory.mktemp("conversation-sessions")
    isolated_sessions = open_sessions(conversation_root)
    from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository

    runtime_root = tmp_path_factory.mktemp("session-runtime")
    repository = SessionRuntimeRepository(str(runtime_root / "astb.sqlite3"))
    registry = SessionRuntimeRegistry(
        repository,
        identity_factory=previous_registry.identity_factory,
    )
    isolated_sessions.bind_runtime_lifecycle(registry.ensure_runtime)
    object.__setattr__(runtime, "sessions", isolated_sessions)
    object.__setattr__(runtime, "session_runtimes", registry)
    runtime.broker.runtime_registry = registry
    object.__setattr__(runtime.actions, "session_runtimes", registry)
    catalog.runtime_registry = registry
    if getattr(catalog, "mutation", None) is not None:
        catalog.mutation.runtime_registry = registry
    kernel.registry = registry
    try:
        yield
    finally:
        object.__setattr__(runtime, "sessions", previous)
        object.__setattr__(runtime, "session_runtimes", previous_registry)
        runtime.broker.runtime_registry = previous_registry
        object.__setattr__(runtime.actions, "session_runtimes", previous_registry)
        catalog.runtime_registry = previous_catalog_registry
        if getattr(catalog, "mutation", None) is not None:
            catalog.mutation.runtime_registry = previous_mutation_registry
        kernel.registry = previous_kernel_registry
        previous.bind_runtime_lifecycle(previous_registry.ensure_runtime)


@pytest.fixture(autouse=True)
def _isolate_automation_history(tmp_path):
    """Redirect AppHost automation history to a per-test file."""
    app = _loaded_app()
    if app is None or app.automation_history is None:
        yield
        return
    from automation import history as automation_history

    previous = app.automation_history
    app.automation_history = automation_history.AutomationHistoryStore(
        str(tmp_path / "automation_history" / "runs.json")
    )
    try:
        yield
    finally:
        app.automation_history = previous


