"""Contracts for the single Browser core exposed through VARIANT-1's kernel."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from artifacts import ContentAddressedArtifactStore
from browser_fabric import (
    BrowserBinding,
    bind_browser_binding,
    bind_browser_fabric,
    close_browser_binding,
    create_browser_fabric,
    create_child_browser_binding_snapshot,
    current_browser_binding,
    ensure_browser_binding,
)
from capability_broker import _CURRENT_INVOCATION
from run_context import Variant1RunContext, bind_run_context
from test_browser_fabric_phase4 import (
    FakeBrowserAdapter,
    FakeCapabilityBroker,
    capability_context,
)
import browser_fabric.capabilities as browser_capabilities
import tools as core_tools
import tools_web as tools
from work_fabric.scope import WorkScope


def _seed_fabric(tmp_path):
    adapters = []

    def factory(kind, profile, session):
        adapter = FakeBrowserAdapter()
        adapter.kind = kind
        adapters.append(adapter)
        return adapter

    fabric = create_browser_fabric(
        data_dir=str(tmp_path / "data"),
        artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
        adapter_factory=factory,
    )
    return fabric, adapters


@pytest.mark.asyncio
async def test_navigation_and_followup_observation_have_distinct_idempotency_keys(tmp_path):
    fabric, adapters = _seed_fabric(tmp_path)
    binding = BrowserBinding(scope=WorkScope(chat_id="phase-keys"))
    with bind_browser_fabric(fabric), bind_browser_binding(binding):
        args = {"url": "https://example.test", "idempotency_key": "fresh-key"}
        first = await tools.browser_navigate(args)
        second = await tools.browser_navigate(args)
        assert first == second
        assert len(adapters[0].perform_calls) == 1
        assert adapters[0].observe_calls == 1
        with pytest.raises(core_tools.ToolError, match="idempotency"):
            await tools.browser_navigate({**args, "url": "https://different.test"})


def test_browser_binding_checkpoint_contains_identity_not_live_browser_state():
    binding = BrowserBinding(
        binding_id="browser_binding_test",
        fabric_session_id="browser_durable",
        owner_kind="run",
        owner_id="run-1",
        scope=WorkScope(chat_id="chat-1", branch_id="branch-1"),
        resume_url="https://example.test/current",
    )

    raw = binding.to_dict()

    assert raw["schema"] == "variant1.browser-binding.v1"
    assert raw["fabric_session_id"] == "browser_durable"
    assert raw["scope"]["branch_id"] == "branch-1"
    assert set(raw) == {
        "schema", "binding_id", "fabric_session_id", "parent_binding_id",
        "owner_kind", "owner_id", "scope", "resume_url", "created_at",
        "updated_at",
    }
    assert not {
        "playwright", "browser", "context", "page", "tabs", "backend",
        "observed_elements", "cleanup_tasks",
    }.intersection(raw)


def test_browser_binding_ignores_deleted_legacy_checkpoint_fields():
    restored = BrowserBinding.from_mapping({
        "session_id": "browser_legacy_registry_row",
        "current_url": "https://example.test/resume",
        "scope": {"chat_id": "chat-1"},
    })

    assert restored.fabric_session_id == ""
    assert restored.resume_url == ""
    assert restored.scope.chat_id == "chat-1"


def test_browser_binding_context_is_nested_and_task_local():
    first = BrowserBinding(binding_id="browser_binding_first")
    second = BrowserBinding(binding_id="browser_binding_second")

    assert current_browser_binding() is None
    with bind_browser_binding(first):
        assert current_browser_binding() is first
        with bind_browser_binding(second):
            assert current_browser_binding() is second
        assert current_browser_binding() is first
    assert current_browser_binding() is None


@pytest.mark.asyncio
async def test_child_binding_inherits_only_authoritative_url(tmp_path):
    fabric, _adapters = _seed_fabric(tmp_path)
    parent_scope = WorkScope(chat_id="chat-parent", branch_id="branch-parent")
    child_scope = WorkScope(chat_id="chat-parent", branch_id="branch-child")
    session = await fabric.open_session(scope=parent_scope)
    await fabric.navigate(
        fabric.page_ref(session.session_id),
        "https://example.test/dashboard",
        scope=parent_scope,
    )
    parent = BrowserBinding(
        binding_id="browser_binding_parent",
        fabric_session_id=session.session_id,
        owner_kind="chat",
        scope=parent_scope,
    )

    with bind_browser_fabric(fabric), bind_browser_binding(parent):
        child_snapshot = create_child_browser_binding_snapshot(scope=child_scope)
        child = ensure_browser_binding(child_snapshot, source="subagent")

    assert child.binding_id != parent.binding_id
    assert child.parent_binding_id == parent.binding_id
    assert child.fabric_session_id == ""
    assert child.resume_url == "https://example.test/dashboard"
    assert child.scope == child_scope


def test_child_binding_can_explicitly_start_without_parent_target():
    parent = BrowserBinding(resume_url="https://example.test/private")
    with bind_browser_binding(parent):
        child = create_child_browser_binding_snapshot(inherit_target=False)
    assert child["fabric_session_id"] is None
    assert child["resume_url"] is None


def test_browser_methods_share_one_atomic_object_slot():
    from session_catalog.catalog import build_catalog_document

    registry = core_tools.ToolRegistry()
    core_tools.register_builtins(
        registry, web_search_handler=lambda _args: None
    )
    document = build_catalog_document(registry)
    explore = next(
        row for row in document["categories"] if row["category_id"] == "explore"
    )
    browser_slot = explore["slots"][1]

    assert browser_slot["projection"] == "object"
    assert browser_slot["primary_namespace"] == "browser"
    assert browser_slot["primary_alias"] == "browser"
    assert {method["alias"] for method in browser_slot["methods"]} == {
        "navigate", "read", "screenshot", "click", "fill",
    }
    assert len(browser_slot["bindings"]) == 1


def test_browser_navigate_method_has_no_legacy_mega_dispatcher():
    registry = core_tools.ToolRegistry()
    core_tools.register_builtins(
        registry, web_search_handler=lambda _args: None
    )
    navigate = next(
        row for row in registry.get("browser").object_methods
        if row["name"] == "navigate"
    )
    params = navigate["params"]

    assert params["url"]["required"] is True
    assert "op" not in params
    assert "action" not in params
    assert "trace_id" not in params


def test_browser_effect_methods_offer_consistent_post_action_screenshots():
    registry = core_tools.ToolRegistry()
    core_tools.register_builtins(
        registry, web_search_handler=lambda _args: None
    )

    methods = {
        row["name"]: row for row in registry.get("browser").object_methods
    }
    assert "include_screenshot" in methods["click"]["params"]
    assert "include_screenshot" in methods["fill"]["params"]


@pytest.mark.asyncio
async def test_five_seeds_share_one_fabric_session(tmp_path):
    import desktop.service as desktop_service

    fabric, adapters = _seed_fabric(tmp_path)
    binding = BrowserBinding(scope=WorkScope(chat_id="chat-seeds"))
    holder = {"image": None}
    image_token = desktop_service.install_image_sink(holder)
    provenance_token = desktop_service.bind_image_provenance(
        "call_browser_screenshot", "browser_screenshot",
    )
    try:
        with bind_browser_fabric(fabric), bind_browser_binding(binding):
            navigated = await tools.browser_navigate({
                "url": "http://127.0.0.1:9222/json",
            })
            read = await tools.browser_read({"max_chars": 1_000})
            clicked = await tools.browser_click({
                "target": "mf_aaaaaaaaaaaa_1",
            })
            await tools.browser_read({"max_chars": 1_000})
            filled = await tools.browser_fill({
                "target": "mf_aaaaaaaaaaaa_1", "text": "hello",
            })
            screenshot = await tools.browser_screenshot({})
    finally:
        desktop_service.reset_image_provenance(provenance_token)
        desktop_service.reset_image_sink(image_token)

    assert len(adapters) == 1
    assert binding.fabric_session_id
    assert "Hello durable browser" in navigated
    assert read.startswith("--- BEGIN UNTRUSTED BROWSER_PAGE CONTENT ---")
    assert clicked == "clicked [mf_aaaaaaaaaaaa_1]"
    assert filled == "filled [mf_aaaaaaaaaaaa_1]"
    assert "image attached to the next model step" in screenshot
    assert [call[1] for call in adapters[0].perform_calls] == [
        "navigate", "click", "fill", "screenshot",
    ]
    assert holder["image"]["origin"] == "tool_result"
    assert holder["image"]["tool_name"] == "browser_screenshot"


@pytest.mark.asyncio
async def test_browser_action_accepts_broker_element_handle_identity(tmp_path):
    fabric, adapters = _seed_fabric(tmp_path)
    binding = BrowserBinding(scope=WorkScope(chat_id="chat-qualified-ref"))

    with bind_browser_fabric(fabric), bind_browser_binding(binding):
        await tools.browser_navigate({"url": "http://127.0.0.1:9222/json"})
        await tools.browser_read({"max_chars": 1_000})
        session = fabric.session(binding.fabric_session_id)
        qualified = f"{session.current_target_id}:mf_aaaaaaaaaaaa_1"
        filled = await tools.browser_fill({"target": qualified, "text": "hello"})

    assert filled == "filled [mf_aaaaaaaaaaaa_1]"
    assert adapters[0].perform_calls[-1][1] == "fill"


def test_browser_seed_uses_embedded_driver_for_interactive_chat():
    ctx = Variant1RunContext.create(source="chat", chat_session=object())
    with bind_run_context(ctx):
        assert tools._default_browser_kind() == "embedded"


def test_browser_seed_uses_managed_driver_for_background_kernel():
    ctx = Variant1RunContext.create(source="subagent")
    with bind_run_context(ctx):
        assert tools._default_browser_kind() == "managed"


@pytest.mark.asyncio
async def test_kernel_seed_returns_fluent_handles_for_absorbed_capabilities(tmp_path):
    from desktop import service as desktop_service

    fabric, adapters = _seed_fabric(tmp_path)
    scope = WorkScope(chat_id="chat-kernel", workspace_id="workspace-kernel")
    context = capability_context(scope)
    registry = core_tools.ToolRegistry()
    broker = FakeCapabilityBroker()
    composed = SimpleNamespace(
        browser=fabric,
        registry=registry,
        broker=broker,
    )
    host = SimpleNamespace(
        require_runtime=lambda: composed,
        remote_handle_routers={},
    )
    browser_capabilities.register_browser_fabric_tools(host)
    binding = BrowserBinding(owner_kind="run", owner_id="run-browser", scope=scope)
    holder = {"image": None}
    image_token = desktop_service.install_image_sink(holder)
    provenance_token = desktop_service.bind_image_provenance(
        context.outer_tool_call_id, "ipython",
    )
    invocation_token = _CURRENT_INVOCATION.set(context)
    try:
        with bind_browser_fabric(fabric, host), bind_browser_binding(binding):
            observed = await tools.browser_navigate({
                "url": "https://example.test/",
                "profile_name": "kernel-profile",
                "include_screenshot": True,
            })
            assert holder["image"]["tool_name"] == "browser_observation"
            holder["image"] = None
            observed = await tools.browser_read({"include_screenshot": True})
            assert holder["image"]["tool_name"] == "browser_observation"
            router = host.remote_handle_routers["browser"]
            session_identity = observed["session"]["$variant1_handle"]
            page_identity = observed["page"]["$variant1_handle"]
            element_identity = observed["elements"][0]["$variant1_handle"]
            pages = await router(context, session_identity, "pages", {})
            clicked = await router(
                context,
                element_identity,
                "click",
                {"include_screenshot": True},
            )
            assert holder["image"]["tool_name"] == "browser_click"
            page_identity = clicked["page"]["$variant1_handle"]
            session_identity = clicked["session"]["$variant1_handle"]
            adapters[0].fail_actions.add("screenshot")
            reloaded = await router(
                context,
                page_identity,
                "navigate",
                {"action": "reload", "include_screenshot": True},
            )
            adapters[0].fail_actions.remove("screenshot")
            assert reloaded["result"]["action"] == "reload"
            assert reloaded["result"]["screenshot"]["status"] == "unavailable"
            session_identity = reloaded["session"]["$variant1_handle"]
            events = await router(
                context, session_identity, "history", {"kind": "events"},
            )
            operations = await router(
                context, session_identity, "history", {"kind": "operations"},
            )
            downloads = await router(
                context, session_identity, "history", {"kind": "downloads"},
            )
            trace = await router(context, session_identity, "start_trace", {})
            traces = await router(
                context, session_identity, "history", {"kind": "traces"},
            )
            stopped = await router(
                context, trace["$variant1_handle"], "stop", {},
            )
    finally:
        _CURRENT_INVOCATION.reset(invocation_token)
        desktop_service.reset_image_provenance(provenance_token)
        desktop_service.reset_image_sink(image_token)

    method_names = {
        row["name"] for row in session_identity["methods"]["items"]
    }
    page_method_names = {
        row["name"] for row in page_identity["methods"]["items"]
    }
    element_method_names = {
        row["name"] for row in element_identity["methods"]["items"]
    }
    assert observed["schema"] == "variant1.browser-observation-result.v1"
    assert method_names == {
        "pages", "new_page", "history", "start_trace", "close",
    }
    assert page_method_names == {
        "observe", "navigate", "keys", "wait", "evaluate", "close",
    }
    assert element_method_names == {"click", "hover"}
    assert pages["current"]["$variant1_handle"]["kind"] == "page"
    assert pages["items"][0]["$variant1_handle"]["kind"] == "page"
    assert events and operations
    assert downloads == []
    assert traces[0]["$variant1_handle"]["kind"] == "trace"
    assert clicked["result"]["action"] == "click"
    assert clicked["result"]["screenshot"]["ref"].startswith("artifact://")
    assert stopped["$variant1_handle"]["metadata"]["state"] == "completed"
    assert "methods" not in stopped["$variant1_handle"]
    assert adapters[0].trace_started is False


@pytest.mark.asyncio
async def test_scope_is_applied_before_browser_list_limits(tmp_path):
    fabric, _adapters = _seed_fabric(tmp_path)
    owner = WorkScope(chat_id="chat-owner", branch_id="branch-owner")
    foreign = WorkScope(chat_id="chat-foreign", branch_id="branch-foreign")
    owner_profile = fabric.create_profile("owner-base", scope=owner)
    foreign_profile = fabric.create_profile("foreign-base", scope=foreign)
    owner_session_ids = []
    for index in range(3):
        record = fabric.store.create_session(
            session_id=f"browser_owner_{index}",
            profile_id=owner_profile.profile_id,
            kind="managed",
            headless=True,
            capabilities=(),
            scope=owner,
        )
        owner_session_ids.append(record.session_id)
    for index in range(110):
        fabric.store.create_session(
            session_id=f"browser_foreign_{index}",
            profile_id=foreign_profile.profile_id,
            kind="managed",
            headless=True,
            capabilities=(),
            scope=foreign,
        )
        fabric.store.create_profile(
            profile_id=f"profile_foreign_{index}",
            name=f"foreign-{index}",
            kind="managed",
            persistent=True,
            user_data_dir="",
            scope=foreign,
        )
    for index in range(2):
        fabric.store.create_profile(
            profile_id=f"profile_owner_{index}",
            name=f"owner-{index}",
            kind="managed",
            persistent=True,
            user_data_dir="",
            scope=owner,
        )

    sessions = fabric.sessions(scope=owner, limit=3)
    profiles = fabric.profiles(scope=owner, limit=3)

    assert {row.session_id for row in sessions} == set(owner_session_ids)
    assert len(profiles) == 3
    assert all(row.scope == owner for row in profiles)


def test_named_profiles_are_independent_per_kernel_scope(tmp_path):
    fabric, _adapters = _seed_fabric(tmp_path)
    first_scope = WorkScope(chat_id="chat-a", branch_id="branch-a")
    second_scope = WorkScope(chat_id="chat-b", branch_id="branch-b")

    first = fabric.create_profile("work", scope=first_scope)
    second = fabric.create_profile("work", scope=second_scope)

    assert first.profile_id != second.profile_id
    assert fabric.create_profile("work", scope=first_scope).profile_id == first.profile_id
    assert [row.profile_id for row in fabric.profiles(scope=first_scope)] == [
        first.profile_id,
    ]
    assert [row.profile_id for row in fabric.profiles(scope=second_scope)] == [
        second.profile_id,
    ]


def test_v1_profile_table_is_migrated_to_scoped_names(tmp_path):
    import json
    import sqlite3
    import time

    from browser_fabric.store import BrowserFabricStore

    path = tmp_path / "browser-v1.sqlite3"
    scope = WorkScope(chat_id="chat-old")
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """CREATE TABLE browser_profile (
                profile_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                persistent INTEGER NOT NULL,
                user_data_dir TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0),
                scope_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                UNIQUE(kind, name)
            )"""
        )
        now = time.time()
        connection.execute(
            """INSERT INTO browser_profile(
                profile_id,name,kind,persistent,user_data_dir,state,revision,
                scope_json,metadata_json,created_at,updated_at,last_error
            ) VALUES(?,?,?,?,?,'active',1,?,?,?,?,'')""",
            (
                "profile_legacy", "work", "managed", 1, "",
                json.dumps(scope.to_dict()), "{}", now, now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    store = BrowserFabricStore(path=str(path))

    assert store.get_profile("profile_legacy").scope == scope
    with sqlite3.connect(path) as migrated:
        columns = {
            row[1] for row in migrated.execute("PRAGMA table_info(browser_profile)")
        }
        assert "scope_key" in columns


@pytest.mark.asyncio
async def test_run_binding_closes_the_actual_fabric_owner(tmp_path):
    fabric, adapters = _seed_fabric(tmp_path)
    scope = WorkScope(chat_id="chat-run-close")
    binding = BrowserBinding(owner_kind="run", owner_id="run-close", scope=scope)
    with bind_browser_fabric(fabric), bind_browser_binding(binding):
        await tools.browser_navigate({"url": "https://example.test/"})

    session_id = binding.fabric_session_id
    await close_browser_binding(binding, fabric=fabric)

    assert fabric.session(session_id, scope=scope).state == "closed"
    assert adapters[0].closed is True
    assert session_id not in fabric._adapters
    assert binding.fabric_session_id == ""


@pytest.mark.asyncio
async def test_chat_binding_is_not_closed_at_the_end_of_one_turn(tmp_path):
    fabric, adapters = _seed_fabric(tmp_path)
    scope = WorkScope(chat_id="chat-persistent")
    binding = BrowserBinding(owner_kind="chat", owner_id="chat-persistent", scope=scope)
    with bind_browser_fabric(fabric), bind_browser_binding(binding):
        await tools.browser_navigate({"url": "https://example.test/"})

    session_id = binding.fabric_session_id
    await close_browser_binding(binding, fabric=fabric)

    assert fabric.session(session_id, scope=scope).state == "active"
    assert adapters[0].closed is False
    await close_browser_binding(binding, fabric=fabric, force=True)


@pytest.mark.asyncio
async def test_new_chat_turn_reconnects_through_fabric_not_a_side_registry(tmp_path):
    scope = WorkScope(chat_id="chat-reconnect", branch_id="branch-reconnect")
    first_fabric, first_adapters = _seed_fabric(tmp_path)
    first_binding = BrowserBinding(owner_kind="chat", scope=scope)
    with bind_browser_fabric(first_fabric), bind_browser_binding(first_binding):
        await tools.browser_navigate({"url": "https://example.test/continued"})
    session_id = first_binding.fabric_session_id
    generation = first_fabric.session(session_id, scope=scope).generation

    # Simulate a host restart: live Playwright ownership ends, while the
    # durable Browser core remains the reconnect authority.
    await first_fabric.shutdown()
    second_fabric, second_adapters = _seed_fabric(tmp_path)
    second_binding = BrowserBinding(owner_kind="chat", scope=scope)
    with bind_browser_fabric(second_fabric), bind_browser_binding(second_binding):
        page = await tools.browser_read({})

    assert first_adapters[0].closed is True
    assert len(second_adapters) == 1
    assert second_binding.fabric_session_id == session_id
    assert second_fabric.session(session_id, scope=scope).generation == generation + 1
    assert "https://example.test/continued" in page
    await close_browser_binding(second_binding, fabric=second_fabric, force=True)
    assert second_fabric.session(session_id, scope=scope).state == "closed"


@pytest.mark.asyncio
async def test_chat_deletion_closes_every_scoped_fabric_session(tmp_path):
    fabric, adapters = _seed_fabric(tmp_path)
    owner = WorkScope(chat_id="chat-delete")
    other = WorkScope(chat_id="chat-keep")
    first = await fabric.open_session(scope=owner)
    second = await fabric.open_session(scope=owner)
    kept = await fabric.open_session(scope=other)

    closed = await fabric.delete_chat(owner.chat_id)

    assert closed == 2
    assert fabric.session(first.session_id, scope=owner).state == "closed"
    assert fabric.session(second.session_id, scope=owner).state == "closed"
    assert fabric.session(kept.session_id, scope=other).state == "active"
    assert [adapter.closed for adapter in adapters] == [True, True, False]
    await fabric.close_session(kept.session_id, scope=other)


def test_no_hidden_browser_service_handlers_are_model_seeds():
    from session_catalog.catalog import COVERED_BROKER_HANDLER_NAMES

    assert {
        "browser_profile_create", "browser_profile_list", "browser_session_list",
        "browser_open", "browser_observe", "browser_action", "browser_tab_list",
        "browser_tab_new", "browser_tab_select", "browser_tab_close",
        "browser_trace_start", "browser_trace_stop", "browser_close",
    }.isdisjoint(COVERED_BROKER_HANDLER_NAMES)
