"""Register VARIANT-1's single model-facing Windows computer-use object.

Desktop Fabric owns window identity, observation, input routing, and receipts.
The model sees ordinary task verbs only; driver and verification policy never
appear in this contract.
"""

from __future__ import annotations

from typing import Any, Mapping

import tools


COMPUTER_OBJECT_METHODS: tuple[dict[str, Any], ...] = (
    {
        "name": "list_windows",
        "description": "List currently open targetable Windows windows.",
        "effect_class": "read",
        "parallel_safe": True,
        "touches_desktop": False,
        "params": {"query": {"type": "string", "required": False}},
    },
    {
        "name": "focus",
        "description": "Focus exactly one returned window or one unambiguous title match.",
        "effect_class": "external_side_effect",
        "touches_desktop": True,
        "params": {
            "name": {"type": "string", "required": False},
            "window": {"type": "any", "required": False},
        },
    },
    {
        "name": "observe",
        "description": "Capture a fresh point-in-time view of one window. When a screenshot is included, view.image.save(path) exports the original image and view.image.read_bytes() returns bytes; image.size is a count.",
        "effect_class": "read",
        "parallel_safe": False,
        "touches_desktop": True,
        "params": {
            "window": {"type": "any", "required": False},
            "include_screenshot": {"type": "boolean", "required": False},
            "include_text": {"type": "boolean", "required": False,
                             "desc": "Set true to populate view.controls. Default false omits text controls."},
        },
    },
    {
        "name": "click",
        "description": (
            "Click a control or view-relative point and return the resulting view "
            "for the next action. Use a control from view.controls, its raw "
            "id/element_ref, or exact name as target; omit x/y when using target. "
            "Set action only for an explicit semantic control operation."
        ),
        "effect_class": "external_side_effect",
        "touches_desktop": True,
        "params": {
            "view": {"type": "any", "required": True},
            "target": {"type": "any", "required": False},
            "x": {"type": "integer", "required": False},
            "y": {"type": "integer", "required": False},
            "button": {
                "type": "string", "required": False,
                "enum": ["left", "right", "middle"],
            },
            "count": {
                "type": "integer", "required": False,
                "minimum": 1, "maximum": 2,
            },
            "action": {
                "type": "string", "required": False,
                "enum": [
                    "invoke", "select", "toggle", "expand", "collapse",
                    "scroll_into_view",
                ],
            },
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type literal text into the focused control and return the resulting "
            "view for the next action."
        ),
        "effect_class": "external_side_effect",
        "touches_desktop": True,
        "params": {
            "view": {"type": "any", "required": True},
            "text": {"type": "string", "required": True},
        },
    },
    {
        "name": "press_key",
        "description": (
            "Press one key or key chord and return the resulting view for the "
            "next action."
        ),
        "effect_class": "external_side_effect",
        "touches_desktop": True,
        "params": {
            "view": {"type": "any", "required": True},
            "keys": {
                "type": "any",
                "required": True,
                "desc": (
                    "One key/chord such as ENTER, CTRL+O or WIN+R, or a sequence "
                    "of named keys. Unknown key/chord names are rejected; "
                    "use type_text for literal text."
                ),
            },
        },
    },
    {
        "name": "set_value",
        "description": (
            "Replace one editable control value and return the resulting view "
            "for the next action."
        ),
        "effect_class": "external_side_effect",
        "touches_desktop": True,
        "params": {
            "view": {"type": "any", "required": True},
            "target": {"type": "any", "required": True},
            "value": {"type": "string", "required": True},
        },
    },
    {
        "name": "scroll",
        "description": (
            "Scroll vertically from one view-relative point and return the "
            "resulting view for the next action. Positive delta scrolls down; negative scrolls up."
        ),
        "effect_class": "external_side_effect",
        "touches_desktop": True,
        "params": {
            "view": {"type": "any", "required": True},
            "x": {"type": "integer", "required": True},
            "y": {"type": "integer", "required": True},
            "delta": {"type": "integer", "required": True,
                      "desc": "Nonzero signed strength: 100 units per wheel notch, rounded down in magnitude, minimum 1 and maximum 20 notches per call. OS settings determine lines per notch."},
        },
    },
    {
        "name": "drag",
        "description": (
            "Drag between two view-relative points and return the resulting view "
            "for the next action."
        ),
        "effect_class": "external_side_effect",
        "touches_desktop": True,
        "params": {
            "view": {"type": "any", "required": True},
            "from_x": {"type": "integer", "required": True},
            "from_y": {"type": "integer", "required": True},
            "to_x": {"type": "integer", "required": True},
            "to_y": {"type": "integer", "required": True},
        },
    },
)


def _root_params() -> dict[str, dict[str, Any]]:
    params: dict[str, dict[str, Any]] = {
        "operation": {
            "type": "string",
            "required": True,
            "enum": [str(method["name"]) for method in COMPUTER_OBJECT_METHODS],
        }
    }
    for method in COMPUTER_OBJECT_METHODS:
        for name, raw in dict(method.get("params") or {}).items():
            spec = dict(raw)
            spec["required"] = False
            current = params.get(str(name))
            if current is not None and current != spec:
                raise RuntimeError(f"conflicting computer parameter schema: {name}")
            params[str(name)] = spec
    return params


def register(registry: tools.ToolRegistry, control: Any) -> None:
    """Register one broker transport projected as the ``computer`` object."""

    async def computer(arguments: Mapping[str, Any]):
        from desktop_fabric.access import current_desktop_host
        from desktop_fabric.capabilities import desktop_computer_operation

        host = current_desktop_host(control)
        if host is None:
            raise tools.ToolError("Desktop Fabric is unavailable")
        return await desktop_computer_operation(host, dict(arguments or {}))

    registry.register(tools.Tool(
        "computer",
        "Observe and control one exact Windows window; every action returns the "
        "resulting view for the next action. input_sent reports delivery; "
        "inspect the returned view and action_status to verify the requested change.",
        computer,
        category="desktop",
        hidden=True,
        visibility="broker_only",
        effect_class="external_side_effect",
        parallel_safe=False,
        idempotency="none",
        touches_desktop=True,
        may_return_secrets=True,
        params=_root_params(),
        schema_revision="variant1.computer.v5",
        handler_revision="variant1.computer-handler.v5",
        object_methods=COMPUTER_OBJECT_METHODS,
    ))


__all__ = ["COMPUTER_OBJECT_METHODS", "register"]
