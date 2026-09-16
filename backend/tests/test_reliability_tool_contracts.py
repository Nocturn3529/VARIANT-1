"""Contracts prompted by concrete recovered mistakes in Astra acceptance."""
import json

import pytest

from extensions.capabilities_v2 import mcp_connector_methods
from kernel_runtime.worker_bridge import Variant1BrowserResult, Variant1BrowserObservation


@pytest.mark.parametrize("kind,default", [
    ("tool", "call"), ("resource", "read_resource"),
    ("resource_template", "read_resource"), ("prompt", "get_prompt"),
])
def test_connector_disclosure_matches_lease_operation(kind, default):
    invoke = next(row for row in mcp_connector_methods(kind) if row["name"] == "invoke")
    assert next(p for p in invoke["params"] if p["name"] == "operation")["default"] == default
    assert repr(default) in invoke["description"]
    assert "read_resource" in invoke["description"]
    # Handle-specific projection must not alter another capability's contract.
    tool = next(row for row in mcp_connector_methods("tool") if row["name"] == "invoke")
    assert next(p for p in tool["params"] if p["name"] == "operation")["default"] == "call"


@pytest.mark.parametrize("field", ["elements", "find_elements", "metadata"])
def test_browser_action_wrong_shape_explains_existing_continuation(field):
    action = Variant1BrowserResult({"action": "fill", "result": {"applied": True}, "page": "page"})
    with pytest.raises(AttributeError, match=r"result.page.observe\(\)"):
        getattr(action, field)
    assert action.applied is True
    assert "Fresh elements: .page.observe()" in repr(action)
    assert json.loads(json.dumps(dict(action)))["applied"] is True
    observation = Variant1BrowserObservation({"snapshot": {}, "elements": []})
    assert observation.elements == []
