"""Promoted helpers keep working when they name mounted capabilities differently."""

from __future__ import annotations

from kernel_runtime.worker_bridge import ReadOnlyTools, _promoted_helper_contract

_TOOLS = ReadOnlyTools([{
    "alias": "read_file",
    "params": {"path": {"type": "string", "required": True}},
    "signature": "read_file(path)",
}], None)
t = _TOOLS
reader = _TOOLS.read_file


def via_namespace_alias(path: str):
    return t.read_file(path)


def via_method_alias(path: str):
    return reader(path)


class _WorkerTools:
    """Stands in for the mutation worker's ``tools`` proxy root."""

    @staticmethod
    def read_file(path):
        return {"read": path}


def _run_packaged(helper, path):
    _name, _schema, source = _promoted_helper_contract(helper)
    namespace = {"tools": _WorkerTools()}
    exec(compile(source, "<packaged>", "exec"), namespace)
    return source, namespace["run"]({"path": path})


def test_namespace_alias_is_rebound_in_packaged_source():
    source, result = _run_packaged(via_namespace_alias, "a.txt")
    assert "t = tools" in source
    assert result == {"read": "a.txt"}


def test_method_alias_is_rebound_in_packaged_source():
    source, result = _run_packaged(via_method_alias, "b.txt")
    assert "reader = tools.read_file" in source
    assert result == {"read": "b.txt"}
