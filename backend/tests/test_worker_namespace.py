import sys
from types import ModuleType, SimpleNamespace

import pytest

from kernel_runtime.worker_bridge import install_document


def _method(alias: str, *, operation: str | None = None) -> dict:
    descriptor = {
        "alias": alias,
        "description": f"Test {alias}",
        "signature": f"{alias}()",
        "effect_class": "read",
        "params": {},
        "handler_name": f"test_{alias}",
    }
    if operation is not None:
        descriptor["fixed_arguments"] = {"operation": operation}
    return descriptor


def _object(name: str, *methods: dict) -> dict:
    return {
        "name": name,
        "summary": f"Test {name} namespace",
        "methods": list(methods),
    }


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        namespace={},
        mounted_namespace_names=(),
        python_api_names=(),
        mounted_object_names=(),
        capsule_runtime=None,
        document={},
        protected_globals={},
    )


def test_catalog_installs_direct_namespaces_without_toolbelt_duplicates(monkeypatch):
    # The real worker owns a dedicated process. This test runs the installer in
    # pytest's shared interpreter, so every touched import shim must be restored
    # during teardown.
    for module_name in ("tools", "toolbelt", "context", "artifacts"):
        monkeypatch.setitem(
            sys.modules,
            module_name,
            sys.modules.get(module_name, ModuleType(module_name)),
        )

    kernel = _context()
    bridge = SimpleNamespace(last_failure=lambda: None)
    document = {
        "schema": "variant1.astb.namespace.v1",
        "catalog_release_id": "release-test",
        "mount_revision": 0,
        "selected_category_id": "context",
        "catalog_index": [],
        "capabilities": [_method("read_file")],
        "services": {},
        "python_apis": {
            "context": _object("context", _method("snapshot")),
        },
        "mounted_objects": {
            "artifacts": _object(
                "artifacts", _method("list", operation="list")
            ),
            "toolbelt": _object(
                "toolbelt",
                _method("mount", operation="mount"),
                _method("mutation_status", operation="mutation_status"),
            ),
        },
    }

    install_document(kernel, bridge, document)

    namespace = kernel.namespace
    assert namespace["tools"].aliases() == ["read_file"]
    assert namespace["context"].methods() == ["snapshot"]
    assert namespace["artifacts"].methods() == ["list"]
    assert namespace["toolbelt"].methods() == [
        "last_failure", "mount", "mutation_status",
    ]
    assert not callable(namespace["toolbelt"])

    # Category APIs and mounted objects have exactly one direct global/import
    # path. They are deliberately not duplicated as toolbelt children.
    assert not hasattr(namespace["toolbelt"], "context")
    assert not hasattr(namespace["toolbelt"], "artifacts")
    assert sys.modules["context"].snapshot is namespace["context"].snapshot
    assert sys.modules["artifacts"].list is namespace["artifacts"].list
    assert sys.modules["toolbelt"].mount is namespace["toolbelt"].mount
    assert "methods=mount,mutation_status" in repr(namespace["toolbelt"])


def test_catalog_rejects_retired_service_namespace_descriptors(monkeypatch):
    monkeypatch.setitem(sys.modules, "tools", ModuleType("tools"))
    kernel = _context()
    document = {
        "schema": "variant1.astb.namespace.v1",
        "capabilities": [],
        "services": {"connectors": [_method("search")]},
    }

    try:
        install_document(kernel, SimpleNamespace(), document)
    except RuntimeError as exc:
        assert "retired service namespace descriptors" in str(exc)
    else:
        raise AssertionError("retired service namespaces must not be installed")


def test_missing_tools_attribute_points_only_to_current_official_globals(monkeypatch):
    for name in ("tools", "toolbelt", "computer", "session"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name, ModuleType(name)))
    kernel = _context()
    document = {
        "schema": "variant1.astb.namespace.v1",
        "capabilities": [_method("read_file")],
        "python_apis": {"session": _object("session", _method("inspect"))},
        "mounted_objects": {"computer": _object("computer", _method("list_windows"))},
    }
    install_document(kernel, SimpleNamespace(), document)
    old_tools = kernel.namespace["tools"]
    for name in ("computer", "session", "toolbelt"):
        with pytest.raises(AttributeError, match=f"{name} is a top-level global; use {name}"):
            getattr(old_tools, name)
        assert not hasattr(old_tools, name)  # A diagnostic, never a second route.
    assert old_tools.aliases() == ["read_file"]
    assert "computer" not in dir(old_tools)
    with pytest.raises(AttributeError, match="No pinned catalog match"):
        old_tools.unknown

    kernel.namespace["computer"] = object()
    with pytest.raises(AttributeError, match="No pinned catalog match"):
        old_tools.computer
    document["mounted_objects"] = {}
    install_document(kernel, SimpleNamespace(), document)
    assert "computer" not in kernel.namespace
    for current in (old_tools, kernel.namespace["tools"]):
        with pytest.raises(AttributeError, match="No pinned catalog match"):
            current.computer
        with pytest.raises(AttributeError, match="session is a top-level global"):
            current.session
