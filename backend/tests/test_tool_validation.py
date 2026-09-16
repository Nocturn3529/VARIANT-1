"""Local recursive validation of model-facing native tool schemas."""

from __future__ import annotations

import pytest

import tools


async def _unused(_args):
    return "unused"


def _patch_tool() -> tools.Tool:
    return tools.Tool(
        "apply_patch",
        "patch",
        _unused,
        params={
            "changes": {
                "type": "array",
                "required": True,
                "minItems": 1,
                "maxItems": 50,
                "coerce_singleton_object": True,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string", "required": True},
                        "action": {
                            "type": "string",
                            "required": True,
                            "enum": ["write", "replace", "delete"],
                        },
                        "content": {"type": "string"},
                    },
                },
            },
        },
    )


def test_nested_enum_and_unknown_properties_are_rejected():
    tool = _patch_tool()

    with pytest.raises(tools.ToolError, match="must be one of"):
        tool.validate_args({"changes": [{"path": "a.txt", "action": "bogus"}]})

    with pytest.raises(tools.ToolError, match="unknown property"):
        tool.validate_args({
            "changes": [{"path": "a.txt", "action": "write", "surprise": True}],
        })


def test_opted_in_array_normalizes_one_object_before_nested_validation():
    tool = _patch_tool()

    assert tool.validate_args({
        "changes": {"path": "a.txt", "action": "write", "content": "hello"},
    }) == {
        "changes": [
            {"path": "a.txt", "action": "write", "content": "hello"},
        ],
    }


def test_nested_required_properties_and_array_bounds_are_rejected():
    tool = _patch_tool()

    with pytest.raises(tools.ToolError, match="non-empty|at least"):
        tool.validate_args({"changes": []})
    with pytest.raises(tools.ToolError, match="at most 50"):
        tool.validate_args({
            "changes": [{"path": f"{index}.txt", "action": "delete"} for index in range(51)],
        })
    with pytest.raises(tools.ToolError, match="missing required"):
        tool.validate_args({"changes": [{"action": "delete"}]})


def test_top_level_scalar_coercion_remains_model_friendly():
    tool = tools.Tool(
        "sample",
        "sample",
        _unused,
        params={
            "count": {"type": "integer", "required": True, "minimum": 1},
            "enabled": {"type": "boolean"},
        },
    )

    assert tool.validate_args({"count": "2", "enabled": "true"}) == {
        "count": 2,
        "enabled": True,
    }
    with pytest.raises(tools.ToolError, match="boolean"):
        tool.validate_args({"count": 2, "enabled": "perhaps"})

    numeric = tools.Tool(
        "numeric", "numeric", _unused,
        params={"value": {"type": "number", "required": True}},
    )
    with pytest.raises(tools.ToolError, match="finite number"):
        numeric.validate_args({"value": "nan"})


def test_computer_action_enum_is_enforced_before_handler_execution():
    tool = tools.Tool(
        "computer",
        "desktop",
        _unused,
        params={
            "action": {
                "type": "string",
                "required": True,
                "enum": ["click", "move", "screenshot"],
            },
        },
    )

    with pytest.raises(tools.ToolError, match="must be one of"):
        tool.validate_args({"action": "launch_missiles"})


def test_optional_null_is_rejected_instead_of_reaching_handler():
    tool = tools.Tool(
        "sample",
        "sample",
        _unused,
        params={"note": {"type": "string", "required": False}},
    )

    with pytest.raises(tools.ToolError, match="null is not allowed"):
        tool.validate_args({"note": None})


def test_any_schema_preserves_mapping_scalar_and_handle_shaped_values():
    tool = tools.Tool(
        "composable",
        "composable",
        _unused,
        params={"target": {"type": "any", "required": True}},
    )

    mapping = {"kind": "element", "id": "element-7"}
    assert tool.validate_args({"target": mapping})["target"] is mapping
    assert tool.validate_args({"target": 7}) == {"target": 7}


def test_computer_object_methods_are_validated_without_legacy_dispatch_shapes():
    import desktop_control
    from desktop.registry import COMPUTER_OBJECT_METHODS

    registry = tools.ToolRegistry()
    desktop_control.DesktopControl().register(registry)
    tool = registry.get("computer")
    assert tool is not None

    with pytest.raises(tools.ToolError, match="unknown argument"):
        tool.validate_args({
            "operation": "drag",
            "path": [{"x": 1}, {"x": 2, "y": 3}],
        })

    methods = {row["name"]: row for row in COMPUTER_OBJECT_METHODS}
    drag_params = methods["drag"]["params"]
    with pytest.raises(tools.ToolError, match="needs 'to_y'"):
        tools.validate_arguments("computer.drag", {
            "view": {"schema": "view"},
            "from_x": 1, "from_y": 2, "to_x": 3,
        }, drag_params)

    click_params = methods["click"]["params"]
    with pytest.raises(tools.ToolError, match="maximum"):
        tools.validate_arguments("computer.click", {
            "view": {"schema": "view"}, "target": 1, "count": 3,
        }, click_params)
