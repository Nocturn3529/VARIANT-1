from host_runtime import ToolRuntime
from host_services import AstbServices, ToolActionRuntime


def test_action_executor_facade_does_not_shadow_host_tool_surface_protocol():
    assert ToolActionRuntime.__name__ == "ToolActionRuntime"
    assert ToolRuntime.__name__ == "ToolRuntime"
    assert ToolActionRuntime is not ToolRuntime


def test_astb_construction_value_has_no_host_alias_fields():
    assert set(AstbServices.__dataclass_fields__) == {
        "registry", "broker", "catalog", "catalog_releases",
        "session_control", "session_runtimes", "kernel", "actions",
        "session_artifacts", "memory_store",
    }
