"""The provider surface is one immutable IPython schema."""

from __future__ import annotations

import copy

from tool_discovery import (
    ToolCatalogSnapshot,
    initial_tools,
    provider_tool_specs,
    schema_hash,
)


SPECS = [{
    "name": "ipython",
    "description": "Execute Python in the persistent session.",
    "category": "infrastructure",
    "params": {"code": {"type": "string"}},
}]


def test_initial_tools_is_the_complete_single_provider_surface():
    assert initial_tools(SPECS) == SPECS


def test_catalog_snapshot_is_immutable_and_stably_hashed():
    source = copy.deepcopy(SPECS)
    snapshot = ToolCatalogSnapshot.from_specs(source)
    source[0]["description"] = "mutated outside"
    exposed = snapshot.specs[0]
    exposed["description"] = "mutated by consumer"

    assert snapshot.names == frozenset({"ipython"})
    assert snapshot.specs[0]["description"] == SPECS[0]["description"]
    assert snapshot.schema_hash == ToolCatalogSnapshot.from_specs(SPECS).schema_hash
    assert snapshot.schema_hash == schema_hash(SPECS)


def test_provider_specs_returns_a_defensive_copy():
    output = provider_tool_specs(SPECS)
    output[0]["name"] = "mutated"
    assert provider_tool_specs(SPECS)[0]["name"] == "ipython"
