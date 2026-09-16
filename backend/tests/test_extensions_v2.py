from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from extensions.manifests_v2 import ExtensionManifestError, load_extension_manifest, source_manifest
from extensions.mcp_v2 import (
    McpRequestConflict,
    McpV2Error,
    McpV2Service,
    StaleMcpLease,
    UnknownMcpEffect,
    preserve_content,
    transport_spec,
)
from extensions.packages_v2 import ExtensionPackageError, ExtensionPackageService
from extensions.runtime_v2 import create_extension_v2_runtime


@pytest.mark.asyncio
async def test_sdk_cancellation_uses_the_actual_wire_request_id():
    import anyio
    from extensions.mcp_session import CancellableClientSession

    incoming_send, incoming_receive = anyio.create_memory_object_stream(10)
    outgoing_send, outgoing_receive = anyio.create_memory_object_stream(10)
    async with incoming_send, outgoing_receive:
        async with CancellableClientSession(incoming_receive, outgoing_send) as session:
            first = asyncio.create_task(session.call_tool("first", {}))
            second = asyncio.create_task(session.call_tool("second", {}))
            requests = [await outgoing_receive.receive(), await outgoing_receive.receive()]
            by_name = {item.message.root.params["name"]: item.message.root.id for item in requests}
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
            notification = await asyncio.wait_for(outgoing_receive.receive(), 1)
            assert notification.message.root.method == "notifications/cancelled"
            assert notification.message.root.params["requestId"] == by_name["second"]
            assert not first.done()
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            notification = await asyncio.wait_for(outgoing_receive.receive(), 1)
            assert notification.message.root.params["requestId"] == by_name["first"]


def test_sdk_resource_contents_are_typed_content_not_metadata():
    from mcp import types
    result = preserve_content(types.ReadResourceResult(contents=[
        types.TextResourceContents(uri="fixture://readme", mimeType="text/plain", text="Read me"),
        types.BlobResourceContents(uri="fixture://blob", mimeType="application/octet-stream", blob="AAEC"),
    ]))
    assert [block["type"] for block in result.content] == ["resource", "resource"]
    assert result.content[0]["resource"] == {"uri": "fixture://readme", "mimeType": "text/plain", "text": "Read me"}
    assert result.content[1]["resource"]["blob"] == "AAEC"
    assert "contents" not in result.metadata


def _package(root: Path, version: str, *, body: str = "hello") -> Path:
    root.mkdir(parents=True)
    (root / "skills" / "demo").mkdir(parents=True)
    (root / "skills" / "demo" / "SKILL.md").write_text(f"# Demo\n{body}", encoding="utf-8")
    (root / "schemas").mkdir()
    (root / "schemas" / "echo.json").write_text(json.dumps({"type": "object"}), encoding="utf-8")
    manifest = {
        "schema_version": 2, "id": "com.example.demo", "name": "Demo", "version": version,
        "compatibility": {}, "entrypoints": {"mcp_servers": [
            {"id": "demo-mcp", "transport": "stdio", "command": ["python", "-m", "demo.mcp"]},
        ]},
        "contributes": {
            "skills": [{"id": "demo", "path": "skills/demo/SKILL.md"}],
            "capabilities": [{"id": "demo.echo", "handler": "demo:echo",
                              "input_schema": "schemas/echo.json", "effect_class": "read"}],
            "panels": [], "commands": [],
        }, "permissions": ["files.read"],
    }
    (root / "variant1.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _service(tmp_path):
    return ExtensionPackageService(str(tmp_path / "extensions.sqlite3"), str(tmp_path / "store"),
                                   environment_builder=lambda *_: {"mode": "fake"})


def test_model_extension_surface_is_one_connectors_object():
    from extensions.capabilities_v2 import register_extension_v2_tools
    from tools import ToolRegistry

    registry = ToolRegistry()
    extensions = SimpleNamespace()
    runtime = SimpleNamespace(
        extensions=extensions,
        registry=registry,
        broker=object(),
    )
    host = SimpleNamespace(
        remote_handle_routers={},
        require_runtime=lambda: runtime,
    )
    register_extension_v2_tools(host)

    assert {tool.name for tool in registry.all()} == {"connectors"}
    assert [row["name"] for row in registry.get("connectors").object_methods] == [
        "search",
    ]
    assert "connectors" in host.remote_handle_routers


def test_manifest_rejects_escape_and_unknown_contribution(tmp_path):
    root = _package(tmp_path / "bad", "1.0.0")
    value = json.loads((root / "variant1.plugin.json").read_text())
    value["contributes"]["skills"][0]["path"] = "../escape"
    (root / "variant1.plugin.json").write_text(json.dumps(value))
    with pytest.raises(ExtensionManifestError, match="contained"):
        load_extension_manifest(root)


def test_manifest_admits_only_external_effect_messaging_adapters(tmp_path):
    root = _package(tmp_path / "messaging", "1.0.0")
    value = json.loads((root / "variant1.plugin.json").read_text())
    value["contributes"]["messaging_adapters"] = [{
        "id": "matrix",
        "adapter_name": "matrix",
        "handler": "matrix_adapter:dispatch",
        "effect_class": "external_effect",
        "poll_interval_s": 2,
    }]
    (root / "variant1.plugin.json").write_text(json.dumps(value))

    manifest = load_extension_manifest(root)
    assert manifest.contributions["messaging_adapters"][0]["handler"] == (
        "matrix_adapter:dispatch"
    )

    value["contributes"]["messaging_adapters"][0]["effect_class"] = "read"
    (root / "variant1.plugin.json").write_text(json.dumps(value))
    with pytest.raises(ExtensionManifestError, match="external_effect"):
        load_extension_manifest(root)


def test_install_upgrade_rollback_pin_and_immutable_version(tmp_path):
    service = _service(tmp_path)
    v1 = service.install(str(_package(tmp_path / "v1", "1.0.0", body="one")))
    assert v1["active"] and v1["immutable"] and len(v1["contributions"]) == 3
    assert {item["kind"] for item in v1["contributions"]} == {"skills", "capabilities", "mcp_server"}
    resource = service.read_resource("com.example.demo", "demo", "SKILL.md")
    assert resource["text"].startswith("# Demo")
    pin = service.pin("chat-1", "com.example.demo")
    v2 = service.update(str(_package(tmp_path / "v2", "2.0.0", body="two")))
    assert v2["active"] and v2["package_digest"] != v1["package_digest"]
    assert service.list_contributions("com.example.demo", chat_id="chat-1")[0]["package_digest"] == pin["package_digest"]
    rolled = service.rollback("com.example.demo", version="1.0.0")
    assert rolled["package_digest"] == v1["package_digest"] and rolled["active"]
    changed = _package(tmp_path / "changed", "1.0.0", body="different")
    with pytest.raises(ExtensionPackageError, match="immutable"):
        service.install(str(changed))


def test_extension_source_links_are_rejected_before_copy_or_publish(tmp_path):
    source = _package(tmp_path / "linked-source", "1.0.0")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("unrelated user data", encoding="utf-8")
    try:
        (source / "skills" / "external").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    with pytest.raises(ExtensionManifestError, match="link or reparse"):
        source_manifest(source)
    service = _service(tmp_path)
    with pytest.raises(ExtensionManifestError, match="link or reparse"):
        service.install(str(source))
    assert service.search() == []


def test_extension_copy_must_match_preflight_digest(tmp_path, monkeypatch):
    import extensions.packages_v2 as packages_module

    source = _package(tmp_path / "changing-source", "1.0.0")
    service = _service(tmp_path)
    original_copy = packages_module.shutil.copy2
    changed = False

    def change_during_copy(src, dst, *args, **kwargs):
        nonlocal changed
        result = original_copy(src, dst, *args, **kwargs)
        if not changed:
            changed = True
            (source / "skills" / "demo" / "SKILL.md").write_text(
                "# Demo\nchanged after preflight", encoding="utf-8",
            )
        return result

    monkeypatch.setattr(packages_module.shutil, "copy2", change_during_copy)
    with pytest.raises(ExtensionPackageError, match="changed during staging"):
        service.install(str(source))
    assert service.search() == []


@pytest.mark.asyncio
async def test_cancelled_extension_build_cannot_publish_or_activate_later(tmp_path):
    source = _package(tmp_path / "cancelled-package", "1.0.0")
    (source / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
    build_entered = threading.Event()
    release_build = threading.Event()
    cancellation = threading.Event()

    def builder(_source, environment, _lock):
        build_entered.set()
        release_build.wait(5)
        environment.mkdir(parents=True, exist_ok=True)
        return {"mode": "test"}

    service = ExtensionPackageService(
        str(tmp_path / "extensions.sqlite3"),
        str(tmp_path / "store"),
        environment_builder=builder,
    )
    installing = asyncio.create_task(asyncio.to_thread(
        service.install,
        str(source),
        cancellation_requested=cancellation.is_set,
    ))
    for _ in range(200):
        if build_entered.is_set():
            break
        await asyncio.sleep(0.005)
    assert build_entered.is_set()
    cancellation.set()
    release_build.set()
    with pytest.raises(ExtensionPackageError, match="cancelled"):
        await installing

    assert service.search() == []
    with pytest.raises(LookupError):
        service.inspect("com.example.demo")


def test_default_extension_builder_forwards_cancellation_to_pip(
    tmp_path, monkeypatch,
):
    from execution_hosts.local import BoundedChildResult
    import extensions.packages_v2 as packages_module

    source = tmp_path / "pip-source"
    source.mkdir()
    (source / "requirements.txt").write_text("example==1.0\n", encoding="utf-8")
    environment = tmp_path / "environment"
    cancellation = threading.Event()
    cancellation_requested = cancellation.is_set
    observed = {}

    def fake_child(_argv, **kwargs):
        observed.update(kwargs)
        return BoundedChildResult(-1, b"", b"", cancelled=True)

    monkeypatch.setattr(packages_module, "run_bounded_child", fake_child)
    with pytest.raises(ExtensionPackageError, match="cancelled"):
        packages_module.build_extension_environment(
            source,
            environment,
            source / "requirements.txt",
            cancellation_requested=cancellation_requested,
        )

    assert observed["cancellation_requested"] is cancellation_requested
    assert observed["max_stdout_bytes"] == 8 * 1024 * 1024
    assert observed["max_stderr_bytes"] == 8 * 1024 * 1024


def test_dev_mount_is_chat_local_and_does_not_activate(tmp_path):
    service = _service(tmp_path)
    mounted = service.dev_mount(str(_package(tmp_path / "dev", "0.1.0")), chat_id="chat-dev")
    assert mounted["chat_local"] is True and mounted["immutable"] is False
    assert service.list_contributions("com.example.demo") == []
    assert len(service.list_contributions("com.example.demo", chat_id="chat-dev")) == 3
    assert service.read_resource("com.example.demo", "demo", "SKILL.md",
                                 chat_id="chat-dev")["text"].startswith("# Demo")


class _FakeSession:
    def __init__(self):
        self.block = asyncio.Event()
        self.subscribed = []
        self.tool_calls = []
        self.dynamic_unlocked = False

    async def list_tools(self):
        tools = [{"name": "echo", "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                            "annotations": {"readOnlyHint": True}}]
        if self.dynamic_unlocked:
            tools.append({
                "name": "finish",
                "inputSchema": {
                    "type": "object",
                    "properties": {"response_code": {"type": "string"}},
                    "required": ["response_code"],
                },
                "annotations": {"readOnlyHint": False},
            })
        return {"tools": tools}

    async def list_resources(self):
        return {"resources": [{"uri": "demo://readme", "name": "readme"}]}

    async def list_resource_templates(self): return {"resourceTemplates": []}
    async def list_prompts(self): return {"prompts": [{"name": "review", "arguments": []}]}

    async def call_tool(self, name, arguments, progress_callback=None):
        self.tool_calls.append((name, dict(arguments)))
        if progress_callback is not None:
            await progress_callback({"progress": 1, "total": 1})
        if arguments["text"] == "error":
            return {
                "content": [{"type": "text", "text": "synthetic MCP failure"}],
                "isError": True,
            }
        if arguments["text"] == "unlock":
            self.dynamic_unlocked = True
            return {
                "content": [{
                    "type": "text",
                    "text": '{"next_tool":"finish","response_code":"RC-1"}',
                }],
                "isError": False,
            }
        return {"content": [
            {"type": "text", "text": arguments["text"]},
            {"type": "image", "data": "AA==", "mimeType": "image/png"},
            {"type": "resource_link", "uri": "demo://readme", "name": "readme"},
        ], "structuredContent": {"ok": True}, "isError": False}

    async def read_resource(self, uri):
        return {"content": [{"type": "resource", "resource": {"uri": uri, "text": "body"}}]}

    async def get_prompt(self, name, arguments):
        return {"messages": [{"role": "user", "content": {"type": "text", "text": name}}],
                "arguments": arguments}

    async def subscribe_resource(self, uri): self.subscribed.append(uri)
    async def unsubscribe_resource(self, uri):
        if uri in self.subscribed: self.subscribed.remove(uri)


@pytest.mark.asyncio
async def test_connectors_search_returns_bound_mcp_handle(tmp_path):
    from capability_broker import CapabilityBroker, InvocationContext
    from extensions.capabilities_v2 import register_extension_v2_tools
    from tools import ToolRegistry
    from work_fabric.capabilities import register_work_fabric_tools
    from work_fabric.scope import WorkScope
    from work_fabric.service import WorkService
    from tests.support.astb_runtime import StaticRuntimeRegistry

    session = _FakeSession()
    mcp = McpV2Service(
        opener=lambda _spec: asyncio.sleep(0, result=session), deadline_s=1,
    )
    await mcp.connect(
        "demo", {"transport": "streamable_http", "url": "https://example.test/mcp"}
    )
    registry = ToolRegistry()
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    extensions = SimpleNamespace(
        mcp=mcp,
        packages=_service(tmp_path / "packages"),
        workers=SimpleNamespace(),
    )
    runtime = SimpleNamespace(
        extensions=extensions,
        work=work,
        registry=registry,
    )
    host = SimpleNamespace(
        registry=registry,
        remote_handle_routers={},
        require_runtime=lambda: runtime,
    )
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=StaticRuntimeRegistry(),
        enabled_resolver=lambda: {tool.name for tool in registry.all()},
    )
    runtime.broker = broker
    register_work_fabric_tools(host)
    register_extension_v2_tools(host)
    context = InvocationContext(
        chat_id="chat-connectors", run_id="run-connectors",
        outer_tool_call_id="outer-connectors", cell_execution_id="cell-connectors",
        nested_call_id="search-connectors", surface="ipython",
        catalog_release_id="astb.test.release.v1",
        work_scope=WorkScope(chat_id="chat-connectors"),
    )
    found = await broker.invoke_name(
        "connectors", {"operation": "search", "query": "echo", "kind": "tool"},
        context,
    )
    assert found.ok, found.error
    assert "exact compact schema" in found.result_value["guidance"]
    assert found.result_value["top_match"]["schema"]["name"] == "echo"
    assert found.result_value["top_match"]["schema"]["descriptor"][
        "inputSchema"
    ]["required"] == ["text"]
    top_identity = found.result_value["top_match"]["handle"]["$variant1_handle"]
    assert top_identity["metadata"]["schema_included"] is True
    assert "included beside" in top_identity["metadata"]["usage_hint"]
    envelope = found.result_value["mcp"][0]
    identity = envelope["$variant1_handle"]
    assert identity["metadata"]["name"] == "echo"
    assert identity["metadata"]["schema_included"] is True
    assert "included beside" in identity["metadata"]["usage_hint"]

    def dispatch(method, arguments, nonce):
        return broker.invoke_name(
            "remote_handle_dispatch",
            {
                "handle": {
                    key: identity[key]
                    for key in ("service", "kind", "id", "generation", "revision")
                },
                "method": method,
                "arguments": arguments,
            },
            InvocationContext(**{
                **context.__dict__, "nested_call_id": nonce,
            }),
        )

    schema = await dispatch("schema", {}, "schema-connectors")
    invalid = await dispatch(
        "invoke", {"arguments": {"item_id": "wrong"}}, "invalid-connectors"
    )
    missing = await dispatch("invoke", {"arguments": {}}, "missing-connectors")
    invoked = await dispatch(
        "invoke", {"arguments": {"text": "hello"}}, "invoke-connectors"
    )
    concluded = await dispatch(
        "invoke",
        {"arguments": {"text": "finished"}, "conclude": True},
        "conclude-connectors",
    )
    failed = await dispatch(
        "invoke", {"arguments": {"text": "error"}}, "failed-connectors"
    )
    raw_failure = await dispatch(
        "invoke",
        {"arguments": {"text": "error"}, "raise_on_error": False},
        "raw-failed-connectors",
    )
    unlocked = await dispatch(
        "invoke", {"arguments": {"text": "unlock"}}, "unlock-connectors"
    )
    assert schema.ok and schema.result_value["name"] == "echo"
    assert not invalid.ok and "item_id" in str(invalid.error)
    assert "Exact input contract" in str(invalid.error)
    assert '"text":{"required":true,"type":"string"}' in str(invalid.error)
    assert not missing.ok and "missing required" in str(missing.error)
    assert invoked.ok and invoked.result_value["structured_content"] == {"ok": True}
    assert concluded.ok and concluded.terminate is True
    assert concluded.result_value["structured_content"] == {"ok": True}
    assert concluded.result_metadata["concluded"] is True
    assert '"ok":true' in concluded.result_metadata["terminal_observation"]
    assert not failed.ok and "synthetic MCP failure" in str(failed.error)
    assert failed.error.code == "mcp_tool_error"
    assert "Do not repeat unchanged" not in str(failed.error)
    assert "raise_on_error=False" not in str(failed.error)
    assert raw_failure.ok and raw_failure.result_value["is_error"] is True
    assert unlocked.ok
    next_capability = unlocked.result_value["next_capability"]
    next_identity = next_capability["handle"]["$variant1_handle"]
    assert next_identity["metadata"]["name"] == "finish"
    assert next_identity["metadata"]["schema_included"] is True
    assert next_capability["schema"]["descriptor"]["inputSchema"][
        "required"
    ] == ["response_code"]
    assert session.tool_calls == [
        ("echo", {"text": "hello"}),
        ("echo", {"text": "finished"}),
        ("echo", {"text": "error"}),
        ("echo", {"text": "error"}),
        ("echo", {"text": "unlock"}),
    ]
    refreshed_schema = await dispatch("schema", {}, "stale-schema-refresh")
    assert refreshed_schema.ok, refreshed_schema.to_dict()
    assert refreshed_schema.result_value["refreshed"] is True
    replacement_identity = refreshed_schema.result_value[
        "replacement_handle"
    ]["$variant1_handle"]
    assert replacement_identity["revision"] > identity["revision"]
    assert replacement_identity["metadata"]["schema_included"] is True


@pytest.mark.asyncio
async def test_mcp_v2_leases_resources_prompts_subscriptions_and_content():
    session = _FakeSession()
    service = McpV2Service(opener=lambda _spec: asyncio.sleep(0, result=session), deadline_s=1)
    await service.connect("demo", {"transport": "streamable_http", "url": "https://example.test/mcp"})
    tool = service.lease("demo", "tool", "echo")
    progress = []
    result = await service.call_tool(tool, {"text": "hello"}, progress=progress.append)
    assert [row["type"] for row in result.content] == ["text", "image", "resource_link"]
    assert result.structured_content == {"ok": True}
    assert progress == [{"progress": 1, "total": 1}]
    resource = service.lease("demo", "resource", "demo://readme")
    assert (await service.read_resource(resource)).content[0]["resource"]["text"] == "body"
    assert (await service.subscribe(resource))["subscribed"] is True
    assert (await service.unsubscribe(resource))["subscribed"] is False
    prompt = service.lease("demo", "prompt", "review")
    prompt_result = await service.get_prompt(prompt)
    assert prompt_result.content[0]["text"] == "review"
    assert prompt_result.content[0]["role"] == "user"
    session.list_tools = lambda: asyncio.sleep(0, result={"tools": [{"name": "echo", "inputSchema": {"type": "object", "properties": {"new": {"type": "string"}}}}]})
    await service.refresh("demo")
    with pytest.raises(StaleMcpLease, match="refresh"):
        await service.call_tool(tool, {"text": "old"})


@pytest.mark.asyncio
async def test_mcp_sdk_descriptors_normalize_typed_uris_before_catalog_hashing():
    from mcp import types
    from pydantic import AnyUrl
    from core_invariants import canonical_json, StrictJSONError
    from extensions.mcp_v2 import _dict

    class TypedSession(_FakeSession):
        async def list_resources(self):
            return types.ListResourcesResult(resources=[types.Resource(
                uri=AnyUrl("demo://readme"), name="readme",
                icons=[types.Icon(src="https://example.test/icon.png", mimeType="image/png")],
            )])

        async def list_resource_templates(self):
            return types.ListResourceTemplatesResult(resourceTemplates=[
                types.ResourceTemplate(uriTemplate="demo://{name}", name="named")])

        async def list_prompts(self):
            return types.ListPromptsResult(prompts=[types.Prompt(
                name="review", arguments=[types.PromptArgument(name="topic", required=False)])])

    session = TypedSession()
    service = McpV2Service(opener=lambda _: asyncio.sleep(0, result=session))
    try:
        await service.connect("demo", {"transport": "streamable_http", "url": "https://example.test/mcp"})
        before = service.search("")
        assert {row["kind"] for row in before} == {"tool", "resource", "resource_template", "prompt"}
        resource = service.lease("demo", "resource", "demo://readme")
        assert (await service.read_resource(resource)).content[0]["resource"]["text"] == "body"
        await service.refresh("demo")
        assert service.search("") == before
        normalized = _dict({"nested": [{"uri": AnyUrl("demo://readme")} ]})
        assert normalized == {"nested": [{"uri": "demo://readme"}]}
        with pytest.raises(StrictJSONError):
            canonical_json(_dict({"unsupported": object()}))
    finally:
        await service.disconnect("demo")


@pytest.mark.asyncio
async def test_mcp_revalidates_lease_after_waiting_for_dispatch():
    session = _FakeSession()
    service = McpV2Service(opener=lambda _spec: asyncio.sleep(0, result=session), deadline_s=2)
    await service.connect("demo", {"transport": "streamable_http", "url": "https://example.test/mcp"})
    server = service._require("demo")
    server.semaphore = asyncio.Semaphore(0)
    lease = service.lease("demo", "tool", "echo")
    task = asyncio.create_task(service.call_tool(lease, {"text": "old"}, request_id="queued"))
    try:
        while "queued" not in server.requests:
            await asyncio.sleep(0)
        session.list_tools = lambda: asyncio.sleep(0, result={"tools": []})
        await service.refresh("demo")
        server.semaphore.release()
        with pytest.raises(StaleMcpLease):
            await task
        assert session.tool_calls == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.disconnect_all()


@pytest.mark.asyncio
async def test_mcp_connect_failure_closes_unpublished_transport_owner(monkeypatch):
    class Owner:
        def __init__(self):
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1

    owner = Owner()
    session = _FakeSession()
    service = McpV2Service(
        opener=lambda _spec: asyncio.sleep(0, result=(session, owner)),
        deadline_s=1,
    )
    original_persist = service._persist_server
    persist_calls = 0

    def fail_first_persist(server, *, enabled=True):
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls == 1:
            raise RuntimeError("simulated catalog persistence failure")
        return original_persist(server, enabled=enabled)

    monkeypatch.setattr(service, "_persist_server", fail_first_persist)
    with pytest.raises(RuntimeError, match="catalog persistence"):
        await service.connect(
            "failed", {"transport": "stdio", "command": ["fake"]}
        )

    assert owner.close_calls == 1
    assert service.status("failed")["status"] == "disconnected"


@pytest.mark.asyncio
async def test_overlapping_mcp_connects_transfer_one_transport_owner_at_a_time():
    class Owner:
        def __init__(self):
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1

    first_opened = asyncio.Event()
    release_first = asyncio.Event()
    sessions = []
    owners = []

    async def opener(_spec):
        session = _FakeSession()
        owner = Owner()
        sessions.append(session)
        owners.append(owner)
        if len(owners) == 1:
            first_opened.set()
            await release_first.wait()
        return session, owner

    service = McpV2Service(opener=opener, deadline_s=1)
    first = asyncio.create_task(
        service.connect("same", {"transport": "stdio", "command": ["first"]})
    )
    await asyncio.wait_for(first_opened.wait(), timeout=1)
    second = asyncio.create_task(
        service.connect("same", {"transport": "stdio", "command": ["second"]})
    )
    await asyncio.sleep(0)
    assert len(owners) == 1
    release_first.set()
    await asyncio.gather(first, second)

    assert len(owners) == 2
    assert owners[0].close_calls == 1
    assert owners[1].close_calls == 0
    assert service._servers["same"].session is sessions[1]
    await service.disconnect("same")
    assert owners[1].close_calls == 1


@pytest.mark.asyncio
async def test_mcp_configuration_persists_for_parallel_startup_reconnect(tmp_path):
    sessions = []
    async def opener(_spec):
        session = _FakeSession(); sessions.append(session); return session
    path = str(tmp_path / "mcp.sqlite3")
    first = McpV2Service(opener=opener, database_path=path, deadline_s=1)
    await first.connect("one", {"transport": "sse", "url": "https://example.test/sse"})
    await first.subscribe(first.lease("one", "resource", "demo://readme"))
    # Process shutdown preserves the explicit auto-reconnect setting; a user
    # disconnect intentionally disables it.
    await first.disconnect_all()
    second = McpV2Service(opener=opener, database_path=path, deadline_s=1)
    result = await second.reconnect_all()
    assert result == {"one": True}
    assert second.status("one")["generation"] == 2
    assert sessions[-1].subscribed == ["demo://readme"]


@pytest.mark.asyncio
async def test_explicit_mcp_disconnect_disables_startup_reconnect(tmp_path):
    path = str(tmp_path / "mcp-disabled.sqlite3")
    service = McpV2Service(
        opener=lambda _spec: asyncio.sleep(0, result=_FakeSession()),
        database_path=path,
        deadline_s=1,
    )
    await service.connect(
        "manual", {"transport": "stdio", "command": ["fake"]}
    )
    await service.disconnect("manual")

    reopened = McpV2Service(
        opener=lambda _spec: asyncio.sleep(0, result=_FakeSession()),
        database_path=path,
        deadline_s=1,
    )
    assert reopened.configured() == []
    assert await reopened.reconnect_all() == {}
    # A deliberate Reconnect command is still allowed to re-enable it.
    assert (await reopened.reconnect("manual"))["status"] == "connected"


@pytest.mark.asyncio
async def test_mcp_transport_death_revokes_live_catalog_immediately():
    class Owner:
        callback = None

        def set_disconnect_callback(self, callback):
            self.callback = callback

        async def aclose(self):
            pass

    owner = Owner()
    service = McpV2Service(
        opener=lambda _spec: asyncio.sleep(
            0, result=(_FakeSession(), owner)
        ),
        deadline_s=1,
    )
    await service.connect(
        "volatile", {"transport": "stdio", "command": ["fake"]}
    )
    assert service.catalog("volatile")
    await owner.callback(RuntimeError("transport exited"))

    assert service.catalog() == []
    assert service.status("volatile")["status"] == "disconnected"
    with pytest.raises(McpV2Error, match="not connected"):
        service.lease("volatile", "tool", "echo")


@pytest.mark.asyncio
async def test_mcp_custom_opener_is_bounded_by_service_deadline():
    async def hung(_spec):
        await asyncio.sleep(10)

    service = McpV2Service(opener=hung, deadline_s=0.01)
    with pytest.raises(asyncio.TimeoutError):
        await service.connect(
            "hung", {"transport": "stdio", "command": ["fake"]}
        )


@pytest.mark.asyncio
async def test_mcp_read_calls_are_bounded_concurrent_and_explicitly_cancellable():
    class Concurrent(_FakeSession):
        def __init__(self):
            super().__init__(); self.active = 0; self.maximum = 0; self.release = asyncio.Event()

        async def call_tool(self, name, arguments, progress_callback=None):
            self.active += 1; self.maximum = max(self.maximum, self.active)
            try: await self.release.wait()
            finally: self.active -= 1
            return {"content": [{"type": "text", "text": str(arguments["n"])}]}

    session = Concurrent()
    service = McpV2Service(opener=lambda _spec: asyncio.sleep(0, result=session),
                           max_concurrency=3, deadline_s=2)
    await service.connect("parallel", {"transport": "stdio", "command": ["fake"]})
    lease = service.lease("parallel", "tool", "echo")
    tasks = [asyncio.create_task(service.call_tool(lease, {"n": index}, request_id=f"r{index}"))
             for index in range(3)]
    for _ in range(20):
        if session.maximum == 3: break
        await asyncio.sleep(0)
    assert session.maximum == 3
    assert await service.cancel("parallel", "r0") is True
    session.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert [result.content[0]["text"] for result in results[1:]] == ["1", "2"]


@pytest.mark.asyncio
async def test_mcp_caller_key_joins_live_and_replays_after_reconnect(tmp_path):
    class Effectful(_FakeSession):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def list_tools(self):
            return {"tools": [{
                "name": "write",
                "inputSchema": {"type": "object"},
                "annotations": {},
            }]}

        async def call_tool(self, name, arguments, progress_callback=None):
            self.calls += 1
            await asyncio.sleep(0.02)
            return {
                "content": [{"type": "text", "text": str(arguments["value"])}],
                "structuredContent": {"call": self.calls},
            }

    path = str(tmp_path / "mcp-dedupe.sqlite3")
    session = Effectful()
    first = McpV2Service(
        opener=lambda _spec: asyncio.sleep(0, result=session),
        database_path=path,
        deadline_s=1,
    )
    await first.connect(
        "durable", {"transport": "stdio", "command": ["fake"]}
    )
    lease = first.lease("durable", "tool", "write")
    live = await asyncio.gather(
        first.call_tool(lease, {"value": "one"}, request_id="caller-key"),
        first.call_tool(lease, {"value": "one"}, request_id="caller-key"),
    )
    assert session.calls == 1
    assert [item.structured_content for item in live] == [
        {"call": 1}, {"call": 1}
    ]
    await first.disconnect_all()

    second = McpV2Service(
        opener=lambda _spec: asyncio.sleep(0, result=session),
        database_path=path,
        deadline_s=1,
    )
    await second.reconnect_all()
    replay = await second.call_tool(
        second.lease("durable", "tool", "write"),
        {"value": "one"},
        request_id="caller-key",
    )
    assert replay.structured_content == {"call": 1}
    assert session.calls == 1
    with pytest.raises(McpRequestConflict, match="different request"):
        await second.call_tool(
            second.lease("durable", "tool", "write"),
            {"value": "changed"},
            request_id="caller-key",
        )
    assert session.calls == 1


@pytest.mark.asyncio
async def test_mcp_dispatched_effect_becomes_unknown_instead_of_reexecution(tmp_path):
    class Effectful(_FakeSession):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def list_tools(self):
            return {"tools": [{
                "name": "write",
                "inputSchema": {"type": "object"},
                "annotations": {},
            }]}

        async def call_tool(self, name, arguments, progress_callback=None):
            self.calls += 1
            return {"content": [{"type": "text", "text": "changed"}]}

    session = Effectful()
    service = McpV2Service(
        opener=lambda _spec: asyncio.sleep(0, result=session),
        database_path=str(tmp_path / "mcp-unknown.sqlite3"),
        deadline_s=1,
    )
    await service.connect(
        "durable", {"transport": "stdio", "command": ["fake"]}
    )
    lease = service.lease("durable", "tool", "write")
    from core_invariants import request_fingerprint

    fingerprint = request_fingerprint("mcp.call_tool", {
        "server_id": lease.server_id,
        "kind": lease.kind,
        "name": lease.name,
        "schema_digest": lease.schema_digest,
        "args": [lease.name, {"value": "one"}],
    })
    service._reserve_request(
        lease,
        request_id="lost-response",
        method="call_tool",
        fingerprint=fingerprint,
        effectful=True,
    )

    with pytest.raises(UnknownMcpEffect, match="will not be called again"):
        await service.call_tool(
            lease, {"value": "one"}, request_id="lost-response"
        )
    assert session.calls == 0


def test_transport_aliases_and_structured_result_are_not_flattened():
    assert transport_spec({"transport": "http", "url": "https://x"})["transport"] == "streamable_http"
    result = preserve_content({"content": [{"type": "audio", "data": "AA==", "mimeType": "audio/wav"}],
                               "structuredContent": {"rows": [1, 2]}})
    assert result.content[0]["type"] == "audio" and result.structured_content["rows"] == [1, 2]


@pytest.mark.asyncio
async def test_unified_runtime_factory_uses_one_extension_database(tmp_path):
    runtime = create_extension_v2_runtime(
        str(tmp_path), environment_builder=lambda *_: {"mode": "fake"},
        mcp_opener=lambda _spec: asyncio.sleep(0, result=_FakeSession()),
    )
    runtime.packages.install(str(_package(tmp_path / "runtime-plugin", "1.0.0")))
    state = await runtime.start()
    assert state["installed"][0]["package_id"] == "com.example.demo"
    assert state["limitations"]["oauth_pkce"] == "not_implemented"
    await runtime.shutdown()
def test_supported_mcp_client_transport_modules_are_importable():
    from mcp.client.session import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    assert ClientSession is not None
    assert StdioServerParameters is not None
    assert all(callable(item) for item in (
        stdio_client, streamablehttp_client, sse_client,
    ))


@pytest.mark.asyncio
async def test_real_stdio_mcp_transport_closes_in_its_owner_task(tmp_path):
    audit = tmp_path / "audit.jsonl"
    fixture = (
        Path(__file__).resolve().parents[2]
        / "experiments" / "live-canary" / "fixture_mcp_server.py"
    )
    service = McpV2Service(
        deadline_s=10,
        database_path=str(tmp_path / "mcp.sqlite3"),
    )
    server_id = "real-stdio-fixture"
    try:
        connected = await service.connect(server_id, {
            "transport": "stdio",
            "command": [sys.executable, str(fixture)],
            "env": {
                "VARIANT1_ASTB_MCP_RECORDS": json.dumps({"source": "stdio-ready"}),
                "VARIANT1_ASTB_MCP_AUDIT": str(audit),
            },
        })
        assert connected["status"] == "connected"
        lease = service.lease(server_id, "tool", "compose_record")
        result = await service.call_tool(lease, {"record_id": "source"})
        assert "stdio-ready" in json.dumps(result.to_dict())
        assert await service.remove(server_id) is True
        assert '"event": "compose_record"' in audit.read_text(encoding="utf-8")
    finally:
        await service.disconnect_all()


@pytest.mark.asyncio
async def test_stdio_disconnect_reaps_mcp_descendant_not_only_leader(tmp_path):
    import psutil

    marker = tmp_path / "orphan-effect.txt"
    pid_file = tmp_path / "descendant-pid.txt"
    fixture = tmp_path / "mcp_descendant.py"
    child_code = (
        "import time; from pathlib import Path; time.sleep(2.5); "
        f"Path({str(marker)!r}).write_text('survived')"
    )
    fixture.write_text(
        "import subprocess,sys\n"
        "from pathlib import Path\n"
        "from mcp.server.fastmcp import FastMCP\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
        f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "server=FastMCP('Owned descendant fixture')\n"
        "@server.tool()\n"
        "def ping()->str: return 'ready'\n"
        "server.run(transport='stdio')\n",
        encoding="utf-8",
    )
    service = McpV2Service(deadline_s=10)
    descendant = None
    try:
        await service.connect("owned-descendant", {
            "transport": "stdio", "command": [sys.executable, str(fixture)],
        })
        assert pid_file.is_file()
        descendant = psutil.Process(int(pid_file.read_text()))
        started_at = descendant.create_time()
        await service.disconnect("owned-descendant")
        await asyncio.sleep(3)
        assert not marker.exists(), "an MCP grandchild survived disconnect"
        if descendant.is_running() and descendant.create_time() == started_at:
            assert descendant.status() == psutil.STATUS_ZOMBIE
    finally:
        await service.disconnect_all()
        if descendant is not None:
            try:
                if descendant.is_running():
                    descendant.kill()
            except psutil.Error:
                pass
