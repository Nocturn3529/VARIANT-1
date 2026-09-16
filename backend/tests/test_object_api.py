from __future__ import annotations

import inspect
import pytest

from capability_broker import CapabilityBroker
from object_api import dispatch_object, method_arguments, register_object_tool
from tools import ToolError, ToolRegistry
from tests.support.astb_runtime import StaticRuntimeRegistry


METHODS = (
    {
        "name": "read",
        "description": "Read a bounded value.",
        "effect_class": "read",
        "params": {
            "limit": {
                "type": "integer", "required": False,
                "minimum": 1, "maximum": 6,
            },
        },
    },
    {
        "name": "write",
        "description": "Write one value.",
        "effect_class": "write",
        "params": {
            "value": {"type": "string", "required": True},
        },
    },
)


def test_object_registration_has_no_legacy_handler_cleanup_contract():
    assert "legacy_names" not in inspect.signature(register_object_tool).parameters


def test_method_arguments_enforces_selected_method_schema():
    assert method_arguments(
        METHODS, "read", {"operation": "read", "limit": "6"}, api_name="probe"
    ) == {"limit": 6}

    with pytest.raises(ToolError, match="maximum=6"):
        method_arguments(
            METHODS, "read", {"operation": "read", "limit": 100},
            api_name="probe",
        )
    with pytest.raises(ToolError, match="unknown argument"):
        method_arguments(
            METHODS, "read", {"operation": "read", "value": "wrong method"},
            api_name="probe",
        )


def test_one_dispatcher_keeps_per_method_broker_semantics():
    registry = ToolRegistry()

    async def object_handler(args):
        return await dispatch_object(
            METHODS,
            args,
            api_name="probe",
            handlers={
                "read": lambda payload: payload,
                "write": lambda payload: payload,
            },
        )

    register_object_tool(
        registry,
        name="probe",
        description="One mounted object.",
        methods=METHODS,
        handler=object_handler,
        category="test",
        schema_revision="probe.schema.v1",
        handler_revision="probe.handler.v1",
    )
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=StaticRuntimeRegistry(),
        enabled_resolver=lambda: {"probe"},
    )
    tool = registry.get("probe")

    assert {item.name for item in registry.all()} == {"probe"}
    read = broker.metadata_for_tool(tool, {"operation": "read"})
    write = broker.metadata_for_tool(tool, {"operation": "write"})
    assert (read.effect_class, read.parallel_safe, read.idempotency) == (
        "read", True, "naturally_idempotent",
    )
    assert (write.effect_class, write.parallel_safe, write.idempotency) == (
        "write", False, "caller_key",
    )
