from types import SimpleNamespace

import pytest

from extensions.mcp_v2 import McpV2Error, McpV2Service, _Server


@pytest.mark.asyncio
async def test_never_ending_unique_cursors_are_bounded():
    service = McpV2Service()
    calls = 0
    async def page(**_):
        nonlocal calls
        calls += 1
        return {"tools": [], "nextCursor": str(calls)}
    with pytest.raises(McpV2Error, match="100 pages"):
        await service._list_pages(page, "tools")
    assert calls == 100


@pytest.mark.asyncio
async def test_catalog_item_limit_rejects_partial_inventory():
    service = McpV2Service()
    calls = 0
    async def page(**_):
        nonlocal calls
        calls += 1
        return {"tools": [{"name": "row"}] * 2500, "nextCursor": str(calls)}
    with pytest.raises(McpV2Error, match="20000 items"):
        await service._list_pages(page, "tools")
    assert calls == 9


@pytest.mark.asyncio
async def test_failed_partial_refresh_preserves_revision_and_catalog():
    service = McpV2Service()
    server = _Server("server", {}, 1)
    server.catalog = {("tool", "old"): {"name": "old"}}
    server.revision = 7
    async def tools(**_):
        return {"tools": [{"name": "new", "inputSchema": {}}]}
    async def resources(**_):
        raise TimeoutError("incomplete response")
    server.session = SimpleNamespace(list_tools=tools, list_resources=resources)
    with pytest.raises(McpV2Error, match="list_resources"):
        await service._refresh_server(server)
    assert server.revision == 7 and list(server.catalog) == [("tool", "old")]


@pytest.mark.asyncio
async def test_unsupported_optional_inventory_is_not_a_refresh_failure():
    service = McpV2Service()
    server = _Server("server", {}, 1)
    class Unsupported(Exception):
        code = -32601
    async def tools(**_):
        return {"tools": [{"name": "new", "inputSchema": {}}]}
    async def resources(**_):
        raise Unsupported()
    server.session = SimpleNamespace(list_tools=tools, list_resources=resources)
    await service._refresh_server(server)
    assert ("tool", "new") in server.catalog and server.revision == 1
