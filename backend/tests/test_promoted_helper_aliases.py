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


import json as jsonlib  # noqa: E402  (module alias used by a promoted helper)

PREFIX = "note:"


def via_module_alias_and_constant(path: str):
    return jsonlib.dumps({"path": PREFIX + path})


def _cell_defined(path):
    return path.upper()


_cell_defined.__module__ = "__main__"  # as if defined in a kernel cell


def via_cell_defined_helper(path: str):
    return _cell_defined(path)


def test_module_alias_and_json_constant_are_packaged():
    source, result = _run_packaged(via_module_alias_and_constant, "c.txt")
    assert "import json as jsonlib" in source
    assert "PREFIX = 'note:'" in source
    assert result == '{"path": "note:c.txt"}'


def test_cell_defined_helper_gives_an_actionable_error():
    import pytest

    with pytest.raises(ValueError, match="'_cell_defined' cannot be packaged"):
        _promoted_helper_contract(via_cell_defined_helper)
