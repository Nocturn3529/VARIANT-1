import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from artifacts import ContentAddressedArtifactStore
from browser_fabric import (
    AdapterDownload,
    AdapterObservation,
    AdapterResult,
    AdapterTarget,
    BROWSER_KINDS,
    BrowserAdapter,
    BrowserBinding,
    BrowserConflict,
    BrowserScopeMismatch,
    BrowserExpectedState,
    ElementRef,
    BrowserStaleReference,
    BrowserUnknownEffect,
    BrowserUnavailable,
    BrowserValidationError,
    EmbeddedBrowserAdapter,
    ManagedPlaywrightAdapter,
    bind_browser_binding,
    bind_browser_fabric,
    create_browser_fabric,
    page_handle_envelope,
    element_handle_envelope,
    session_handle_envelope,
)
from work_fabric.scope import WorkScope
from capability_broker import InvocationContext, _CURRENT_INVOCATION
from core_invariants import InjectedFault, inject_faults, request_fingerprint
from tools import BROWSER_OBJECT_METHODS, ToolError, ToolRegistry
import browser_fabric.capabilities as browser_capabilities
from browser_fabric.adapters import normalize_embedded_target
import tools_web as browser_seed_tools


def test_embedded_target_normalization_matches_visible_host_contract():
    assert normalize_embedded_target("b2-4") == "b2-4"
    assert normalize_embedded_target("[b2-4]") == "b2-4"
    with pytest.raises(BrowserValidationError):
        normalize_embedded_target("mf_deadbeef0000_1")


def test_embedded_handles_advertise_only_executable_methods():
    broker = FakeCapabilityBroker()
    context = capability_context(WorkScope(chat_id="chat-methods"))
    target = SimpleNamespace(
        session_id="browser-1", target_id="page-1", title="Example",
        url="https://example.test", state="active", document_epoch=1,
        observation_revision=2, revision=3,
    )
    page = SimpleNamespace(
        session_id="browser-1", target_id="page-1", generation=1,
        target_revision=3, document_epoch=1, observation_revision=2,
    )
    capabilities = {
        "navigate", "back", "forward", "reload", "observe", "screenshot",
        "click", "fill", "keys", "wait",
    }
    page_payload = page_handle_envelope(
        page, target=target, capabilities=capabilities,
        broker=broker, context=context,
    )["$variant1_handle"]
    page_methods = {item["name"] for item in page_payload["methods"]["items"]}
    assert page_methods == {"observe", "navigate", "keys", "wait"}

    element = ElementRef(
        session_id="browser-1", target_id="page-1", generation=1,
        document_epoch=1, observation_revision=2, backend_ref="b2-4",
        role="slider", name="Seek slider",
    )
    element_payload = element_handle_envelope(
        element, capabilities=capabilities, actions=("click", "keys"),
        broker=broker, context=context,
    )["$variant1_handle"]
    element_methods = {
        item["name"] for item in element_payload["methods"]["items"]
    }
    assert element_methods == {"click", "keys"}


class FakeBrowserAdapter(BrowserAdapter):
    kind = "managed"
    capabilities = frozenset({
        "navigate", "back", "forward", "reload", "observe", "screenshot",
        "click", "fill", "select", "hover", "keys", "wait", "evaluate",
        "tabs", "downloads", "trace",
    })

    def __init__(self):
        self.pages = {}
        self.active = ""
        self.perform_calls = []
        self.observe_calls = 0
        self.max_parallel = 0
        self._parallel = 0
        self.fail_actions = set()
        self.closed = False
        self.trace_started = False

    async def launch(self, targets):
        for target in targets:
            self.pages[target.backend_target_id] = {
                "url": target.url or "about:blank", "title": target.title or "Blank",
            }
        if not self.pages:
            self.pages["fake-page"] = {"url": "about:blank", "title": "Blank"}
        self.active = next(iter(self.pages))
        return await self.targets()

    async def close(self):
        self.closed = True

    async def targets(self):
        return tuple(
            AdapterTarget(key, value["title"], value["url"], key == self.active)
            for key, value in self.pages.items()
        )

    async def observe(
        self, backend_target_id, *, max_chars, max_elements,
        include_html, include_screenshot,
    ):
        self.observe_calls += 1
        page = self.pages[backend_target_id]
        return AdapterObservation(
            title=page["title"], url=page["url"], text="Hello durable browser",
            html="<button>Send</button>" if include_html else "",
            elements=({
                "backend_ref": "mf_aaaaaaaaaaaa_1",
                "role": "button",
                "name": "Send",
                "text": "Send",
                "visible": True,
                "actions": ["click", "hover"],
            },),
            screenshot=b"fake-png" if include_screenshot else b"",
        )

    async def perform(self, backend_target_id, action, params):
        self.perform_calls.append((backend_target_id, action, dict(params)))
        self._parallel += 1
        self.max_parallel = max(self.max_parallel, self._parallel)
        try:
            if action == "wait":
                await asyncio.sleep(0.02)
            if action in self.fail_actions:
                raise RuntimeError("adapter lost after dispatch")
            page = self.pages[backend_target_id]
            navigated = False
            if action == "navigate":
                page["url"] = str(params["url"])
                page["title"] = "Navigated"
                navigated = True
            download = None
            if params.get("expect_download"):
                download = AdapterDownload("report.bin", "https://download.test/report", b"download")
            return AdapterResult(
                value={"ok": True, "action": action}, title=page["title"], url=page["url"],
                navigated=navigated,
                screenshot=b"shot" if action == "screenshot" else b"",
                download=download, targets=await self.targets(),
            )
        finally:
            self._parallel -= 1

    async def new_page(self, backend_target_id, url=""):
        self.pages[backend_target_id] = {"url": url or "about:blank", "title": "New"}
        self.active = backend_target_id
        return AdapterTarget(backend_target_id, "New", url or "about:blank", True)

    async def close_page(self, backend_target_id):
        del self.pages[backend_target_id]
        self.active = next(iter(self.pages), "")

    async def activate_page(self, backend_target_id):
        self.active = backend_target_id
        page = self.pages[backend_target_id]
        return AdapterTarget(backend_target_id, page["title"], page["url"], True)

    async def start_trace(self, options):
        self.trace_started = True

    async def stop_trace(self, path):
        assert self.trace_started
        Path(path).write_bytes(b"playwright-trace")
        self.trace_started = False


class FakeCapabilityBroker:
    def ref_for_name(self, name, *, catalog_release_id="", **_kwargs):
        return SimpleNamespace(
            opaque_id=f"cap-{name}", handler_revision=f"{name}.handler.v1",
            catalog_release_id=catalog_release_id or "catalog-test",
            slot_id="browser-slot", slot_version=1,
        )


def capability_context(scope):
    return InvocationContext(
        chat_id=scope.chat_id, run_id="run-browser", outer_tool_call_id="outer-browser",
        cell_execution_id="cell-browser", nested_call_id="nested-browser",
        catalog_release_id="catalog-browser", mount_revision=2, work_scope=scope,
    )


@pytest.fixture
def browser_runtime(tmp_path):
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
async def test_advertised_browser_kinds_match_service_and_embedded_navigate_works(
    browser_runtime,
):
    navigate = next(
        method for method in BROWSER_OBJECT_METHODS
        if method["name"] == "navigate"
    )
    kind = navigate["params"]["kind"]
    assert set(kind["enum"]) == set(BROWSER_KINDS)
    assert "Omit" in kind["desc"]
    assert "kind='embedded'" in navigate["description"]

    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-advertised-embedded")
    with pytest.raises(
        BrowserValidationError,
        match="supported kinds: embedded, managed",
    ):
        await fabric.open_session(kind="visible", scope=scope)

    context = capability_context(scope)
    registry = ToolRegistry()
    broker = FakeCapabilityBroker()
    composed = SimpleNamespace(browser=fabric, registry=registry, broker=broker)
    host = SimpleNamespace(
        require_runtime=lambda: composed,
        remote_handle_routers={},
    )
    browser_capabilities.register_browser_fabric_tools(host)
    binding = BrowserBinding(
        owner_kind="run",
        owner_id="run-advertised-embedded",
        scope=scope,
    )
    invocation_token = _CURRENT_INVOCATION.set(context)
    try:
        with bind_browser_fabric(fabric, host), bind_browser_binding(binding):
            observed = await browser_seed_tools.browser_navigate({
                "url": "https://embedded-contract.test/",
                "kind": "embedded",
            })
    finally:
        _CURRENT_INVOCATION.reset(invocation_token)

    assert observed["schema"] == "variant1.browser-observation-result.v1"
    assert fabric.session(binding.fabric_session_id).kind == "embedded"
    assert adapters[0].kind == "embedded"
    backend_target_id, action, params = adapters[0].perform_calls[-1]
    assert backend_target_id == fabric.targets(
        binding.fabric_session_id
    )[0].backend_target_id
    assert action == "navigate"
    assert params["url"] == "https://embedded-contract.test/"


@pytest.mark.asyncio
async def test_durable_session_profile_target_and_reopen(browser_runtime):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-1", goal_id="goal-1", step_id="step-1")
    session = await fabric.open_session(
        kind="managed", profile_name="analysis", initial_url="https://example.test/",
        scope=scope,
    )
    page = fabric.page_ref(session.session_id)

    assert session.state == "active"
    assert page.session_id == session.session_id
    assert fabric.profiles()[0].name == "analysis"
    assert fabric.targets(session.session_id)[0].url == "https://example.test/"

    reopened = create_browser_fabric(
        path=fabric.store.path,
        profile_root=fabric.profile_root,
        artifact_store=fabric.artifact_store,
        adapter_factory=lambda *_args: FakeBrowserAdapter(),
    )
    assert reopened.session(session.session_id).scope == scope
    assert reopened.page_ref(session.session_id).target_id == page.target_id


@pytest.mark.asyncio
async def test_implicit_embedded_profile_is_host_scoped_across_chats(
    browser_runtime,
):
    fabric, _adapters = browser_runtime
    first = await fabric.open_session(
        kind="embedded",
        headless=False,
        scope=WorkScope(chat_id="chat-embedded-a"),
    )
    second = await fabric.open_session(
        kind="embedded",
        headless=False,
        scope=WorkScope(chat_id="chat-embedded-b"),
    )

    assert first.profile_id == second.profile_id
    assert first.session_id != second.session_id
    assert first.scope == WorkScope(chat_id="chat-embedded-a")
    profile = fabric.store.get_profile(first.profile_id)
    assert profile.name == "main-deck"
    assert profile.scope.empty
    assert len(_adapters) == 2

    observed = await fabric.observe(fabric.page_ref(first.session_id), scope=WorkScope(chat_id='chat-embedded-a'), include_screenshot=True)
    assert fabric.artifact_store.read_bytes_scoped(observed.screenshot_artifact_ref, 'chat-embedded-a')
    with pytest.raises(PermissionError):
        fabric.artifact_store.read_bytes_scoped(observed.screenshot_artifact_ref, 'chat-embedded-b')


@pytest.mark.asyncio
async def test_named_embedded_profiles_share_partition_but_not_chat_sessions(browser_runtime):
    fabric, _adapters = browser_runtime
    alias = fabric.create_profile(
        "Main Deck profile", kind="embedded",
        scope=WorkScope(chat_id="chat-alias"),
    )
    first = await fabric.open_session(
        kind="embedded", profile_id=alias.profile_id,
        scope=WorkScope(chat_id="chat-alias"),
    )
    second = await fabric.open_session(
        kind="embedded", profile_name="another alias",
        scope=WorkScope(chat_id="chat-other"),
    )

    assert first.session_id != second.session_id
    assert fabric.store.get_profile(first.profile_id).name == "main-deck"
    assert len(_adapters) == 2


@pytest.mark.asyncio
async def test_embedded_navigation_preserves_other_chat_page_reference(
    browser_runtime,
):
    fabric, _adapters = browser_runtime
    first_scope = WorkScope(chat_id="chat-embedded-a")
    second_scope = WorkScope(chat_id="chat-embedded-b")
    first = await fabric.open_session(
        kind="embedded", headless=False, scope=first_scope,
    )
    stale_page = fabric.page_ref(first.session_id)
    second = await fabric.open_session(
        kind="embedded", headless=False, scope=second_scope,
    )

    await fabric.navigate(
        fabric.page_ref(second.session_id),
        "https://second-chat.test/",
        scope=second_scope,
    )

    observed = await fabric.observe(stale_page, scope=first_scope)
    assert observed.url != "https://second-chat.test/"
    with pytest.raises(BrowserScopeMismatch):
        await fabric.observe(stale_page, scope=second_scope)


@pytest.mark.asyncio
async def test_observations_are_monotonic_artifact_backed_and_elements_remain_usable_within_document(browser_runtime):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-observe")
    session = await fabric.open_session(scope=scope)
    page = fabric.page_ref(session.session_id)

    first = await fabric.observe(page, include_html=True, include_screenshot=True, scope=scope)
    element = first.one(role="button", name="Send")
    second = await fabric.observe(fabric.page_ref(session.session_id), scope=scope)

    assert second.revision == first.revision + 1
    assert first.text_artifact_ref.startswith("artifact://sha256/")
    assert first.html_artifact_ref.startswith("artifact://sha256/")
    assert first.screenshot_artifact_ref.startswith("artifact://sha256/")
    assert second.changed_since(first)["text_changed"] is False
    before = len(adapters[0].perform_calls)
    await fabric.click(element, scope=scope)
    assert len(adapters[0].perform_calls) == before + 1


@pytest.mark.asyncio
async def test_navigation_advances_document_epoch_and_invalidates_page_ref(browser_runtime):
    fabric, _adapters = browser_runtime
    scope = WorkScope(chat_id="chat-nav")
    session = await fabric.open_session(scope=scope)
    old_page = fabric.page_ref(session.session_id)

    result = await fabric.navigate(old_page, "https://example.test/new", scope=scope)
    new_page = fabric.page_ref(session.session_id)

    assert new_page.document_epoch == old_page.document_epoch + 1
    assert result["page"]["document_epoch"] == new_page.document_epoch
    with pytest.raises(BrowserStaleReference, match="previous top-level document"):
        await fabric.observe(old_page, scope=scope)


@pytest.mark.asyncio
async def test_repeated_navigation_keeps_observation_revision_monotonic(browser_runtime):
    fabric, _adapters = browser_runtime
    scope = WorkScope(chat_id="chat-renavigate")
    session = await fabric.open_session(scope=scope)

    await fabric.navigate(
        fabric.page_ref(session.session_id),
        "https://example.test/first",
        scope=scope,
    )
    first = await fabric.observe(
        fabric.page_ref(session.session_id), scope=scope,
    )
    await fabric.navigate(
        fabric.page_ref(session.session_id),
        "https://example.test/second",
        scope=scope,
    )
    second = await fabric.observe(
        fabric.page_ref(session.session_id), scope=scope,
    )

    assert second.document_epoch == first.document_epoch + 1
    assert second.revision == first.revision + 1


@pytest.mark.asyncio
async def test_cancelled_effectful_action_leaves_durable_unknown_effect(browser_runtime):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-cancelled-browser-action")
    session = await fabric.open_session(scope=scope)
    page = fabric.page_ref(session.session_id)
    dispatched = asyncio.Event()
    hold = asyncio.Event()

    async def cancelled_after_dispatch(backend_target_id, action, params):
        assert backend_target_id
        assert action == "navigate"
        assert params["url"] == "https://cancelled.test/"
        dispatched.set()
        await hold.wait()

    adapters[0].perform = cancelled_after_dispatch
    action = asyncio.create_task(
        fabric.navigate(page, "https://cancelled.test/", scope=scope)
    )
    await asyncio.wait_for(dispatched.wait(), timeout=1)
    action.cancel()
    with pytest.raises(asyncio.CancelledError):
        await action

    operation = next(
        item for item in fabric.operations(session.session_id)
        if item.kind == "navigate"
    )
    assert operation.state == "unknown_effect"
    assert "cancelled after dispatch" in operation.diagnostic


@pytest.mark.asyncio
async def test_cancelled_launch_closes_local_owner_before_recovery(browser_runtime):
    fabric, _fixture_adapters = browser_runtime
    launch_entered = asyncio.Event()
    created = []
    session_ids = []

    def factory(kind, profile, session):
        adapter = FakeBrowserAdapter()
        adapter.kind = kind
        created.append(adapter)
        session_ids.append(session.session_id)
        if len(created) == 1:
            async def blocked_launch(targets):
                launch_entered.set()
                await asyncio.Event().wait()
            adapter.launch = blocked_launch
        return adapter

    fabric._adapter_factory = factory
    opening = asyncio.create_task(
        fabric.open_session(scope=WorkScope(chat_id="chat-launch-cancel"))
    )
    await asyncio.wait_for(launch_entered.wait(), timeout=1)
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    first_session_id = session_ids[0]
    assert created[0].closed is True
    assert first_session_id not in fabric._adapters
    assert fabric.session(first_session_id).state == "unavailable"

    recovered = await fabric.recover_session(first_session_id)
    assert recovered.state == "active"
    assert len(created) == 2
    assert fabric._adapters == {first_session_id: created[1]}


@pytest.mark.asyncio
async def test_cancelled_close_retains_adapter_until_cleanup_is_terminal(
    browser_runtime,
):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-close-owner")
    session = await fabric.open_session(scope=scope)
    adapter = adapters[0]
    close_entered = asyncio.Event()
    release_close = asyncio.Event()

    async def blocked_close():
        close_entered.set()
        await release_close.wait()
        adapter.closed = True

    adapter.close = blocked_close
    closing = asyncio.create_task(
        fabric.close_session(session.session_id, scope=scope)
    )
    await asyncio.wait_for(close_entered.wait(), timeout=1)
    closing.cancel()
    await asyncio.sleep(0.05)
    assert closing.done() is False
    assert fabric._adapters[session.session_id] is adapter

    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert session.session_id not in fabric._adapters
    assert fabric.session(session.session_id).state == "closed"


@pytest.mark.asyncio
async def test_shutdown_fences_launch_before_adapter_publication(browser_runtime):
    fabric, _fixture_adapters = browser_runtime
    launch_entered = asyncio.Event()
    release_launch = asyncio.Event()
    created = []

    def factory(kind, profile, session):
        adapter = FakeBrowserAdapter()
        adapter.kind = kind
        created.append(adapter)

        async def blocked_launch(targets):
            for target in targets:
                adapter.pages[target.backend_target_id] = {
                    "url": target.url or "about:blank", "title": "Blank",
                }
            adapter.active = next(iter(adapter.pages))
            launch_entered.set()
            await release_launch.wait()
            return await adapter.targets()

        adapter.launch = blocked_launch
        return adapter

    fabric._adapter_factory = factory
    opening = asyncio.create_task(
        fabric.open_session(scope=WorkScope(chat_id="chat-shutdown-launch"))
    )
    await asyncio.wait_for(launch_entered.wait(), timeout=1)
    stopping = asyncio.create_task(fabric.shutdown())
    await asyncio.sleep(0)
    release_launch.set()
    results = await asyncio.gather(opening, stopping, return_exceptions=True)

    assert isinstance(results[0], BrowserUnavailable)
    assert results[1] is None
    assert created[0].closed is True
    assert fabric._adapters == {}


@pytest.mark.asyncio
async def test_same_document_actions_can_reuse_an_observed_element(browser_runtime):
    fabric, _adapters = browser_runtime
    scope = WorkScope(chat_id="chat-dom-change")
    session = await fabric.open_session(scope=scope)
    observation = await fabric.observe(fabric.page_ref(session.session_id), scope=scope)
    element = observation.one(name="Send")

    await fabric.click(element, scope=scope)
    await fabric.click(element, scope=scope)


@pytest.mark.asyncio
async def test_navigation_still_invalidates_an_observed_element(browser_runtime):
    fabric, _adapters = browser_runtime
    scope = WorkScope(chat_id="chat-element-navigation")
    session = await fabric.open_session(scope=scope)
    observation = await fabric.observe(
        fabric.page_ref(session.session_id), scope=scope
    )
    element = observation.one(name="Send")

    await fabric.navigate(
        fabric.page_ref(session.session_id),
        "https://example.test/next",
        scope=scope,
    )

    with pytest.raises(BrowserStaleReference, match="previous top-level document"):
        await fabric.click(element, scope=scope)


@pytest.mark.asyncio
async def test_idempotency_replays_once_and_conflicting_payload_is_rejected(browser_runtime):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-idem")
    session = await fabric.open_session(scope=scope)
    page = fabric.page_ref(session.session_id)

    first = await fabric.screenshot(page, idempotency_key="shot-1", scope=scope)
    second = await fabric.screenshot(page, idempotency_key="shot-1", scope=scope)

    assert first == second
    assert [call[1] for call in adapters[0].perform_calls].count("screenshot") == 1
    await fabric.navigate(fabric.page_ref(session.session_id), "https://a.test", idempotency_key="nav", scope=scope)
    with pytest.raises(BrowserConflict, match="different browser request"):
        await fabric.navigate(fabric.page_ref(session.session_id), "https://b.test", idempotency_key="nav", scope=scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["session_id", "original_page", "current_page"])
async def test_navigation_replays_before_mutable_page_checks(browser_runtime, reference_kind):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-navigation-replay")
    session = await fabric.open_session(scope=scope)
    original = fabric.page_ref(session.session_id)
    expected = BrowserExpectedState(document_epoch=original.document_epoch)
    first = await fabric.navigate(
        original, "https://same.test/", expected=expected,
        idempotency_key="same-navigation", scope=scope,
    )
    reference = {
        "session_id": session.session_id,
        "original_page": original,
        "current_page": fabric.page_ref(session.session_id),
    }[reference_kind]

    replay = await fabric.navigate(
        reference, "https://same.test/", expected=expected,
        idempotency_key="same-navigation", scope=scope,
    )

    assert replay == first
    assert [call[1] for call in adapters[0].perform_calls] == ["navigate"]
    with pytest.raises(BrowserConflict, match="different browser request"):
        await fabric.navigate(
            reference, "https://different.test/",
            idempotency_key="same-navigation", scope=scope,
        )
    with pytest.raises(BrowserStaleReference):
        await fabric.navigate(original, "https://same.test/", scope=scope)


@pytest.mark.asyncio
async def test_navigation_replay_waiting_for_target_lock_uses_original_receipt(browser_runtime):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-concurrent-replay")
    session = await fabric.open_session(scope=scope)
    page = fabric.page_ref(session.session_id)
    lock = fabric._target_locks.setdefault((session.session_id, page.target_id), asyncio.Lock())
    async with lock:
        tasks = [asyncio.create_task(fabric.navigate(
            page, "https://same.test/", idempotency_key="concurrent-navigation", scope=scope,
        )) for _ in range(2)]
        await asyncio.sleep(0)
    first, replay = await asyncio.gather(*tasks)

    assert replay == first
    assert len(adapters[0].perform_calls) == 1


@pytest.mark.asyncio
async def test_embedded_session_replay_retains_operation_owner_scope(browser_runtime):
    fabric, adapters = browser_runtime
    owner = WorkScope(chat_id="chat-replay-owner")
    other = WorkScope(chat_id="chat-replay-other")
    session = await fabric.open_session(kind="embedded", headless=False, scope=owner)
    assert session.scope == owner
    page = fabric.page_ref(session.session_id)
    first = await fabric.navigate(
        page, "https://same.test/", idempotency_key="owned-navigation", scope=owner,
    )

    with pytest.raises(BrowserScopeMismatch):
        await fabric.navigate(
            page, "https://same.test/", idempotency_key="owned-navigation", scope=other,
        )
    assert await fabric.navigate(
        page, "https://same.test/", idempotency_key="owned-navigation", scope=owner,
    ) == first
    assert len(adapters[0].perform_calls) == 1


@pytest.mark.asyncio
async def test_navigation_replay_survives_session_close(browser_runtime):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-closed-replay")
    session = await fabric.open_session(scope=scope)
    original = fabric.page_ref(session.session_id)
    first = await fabric.navigate(
        original, "https://same.test/", idempotency_key="closed-navigation", scope=scope,
    )
    await fabric.close_session(session.session_id, scope=scope)

    replay = await fabric.navigate(
        original, "https://same.test/", idempotency_key="closed-navigation", scope=scope,
    )

    assert replay == first
    assert len(adapters) == 1
    assert adapters[0].closed
    assert len(adapters[0].perform_calls) == 1


@pytest.mark.asyncio
async def test_dispatched_mutation_failure_is_unknown_and_never_auto_retried(browser_runtime):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-unknown")
    session = await fabric.open_session(scope=scope)
    page = fabric.page_ref(session.session_id)
    adapters[0].fail_actions.add("navigate")

    with pytest.raises(BrowserUnknownEffect, match="final browser effect is unknown"):
        await fabric.navigate(page, "https://lost.test", idempotency_key="lost", scope=scope)
    with pytest.raises(BrowserUnknownEffect, match="uncertain effect"):
        await fabric.navigate(page, "https://lost.test", idempotency_key="lost", scope=scope)

    assert [call[1] for call in adapters[0].perform_calls].count("navigate") == 1
    assert fabric.operations(session.session_id)[0].state == "unknown_effect"


@pytest.mark.asyncio
async def test_target_mutations_are_serialized(browser_runtime):
    fabric, adapters = browser_runtime
    session = await fabric.open_session()
    page = fabric.page_ref(session.session_id)

    await asyncio.gather(
        fabric.wait(page, condition="timeout", params={"timeout_ms": 1}),
        fabric.wait(page, condition="timeout", params={"timeout_ms": 1}),
        fabric.wait(page, condition="timeout", params={"timeout_ms": 1}),
    )

    assert adapters[0].max_parallel == 1


@pytest.mark.asyncio
async def test_tab_lifecycle_uses_durable_page_handles(browser_runtime):
    fabric, _adapters = browser_runtime
    scope = WorkScope(chat_id="chat-tabs")
    session = await fabric.open_session(scope=scope)
    first = fabric.page_ref(session.session_id)
    second = await fabric.new_page(session.session_id, url="https://two.test", scope=scope)

    assert len(fabric.targets(session.session_id)) == 2
    assert (await fabric.select_page(first, scope=scope)).target_id == first.target_id
    await fabric.close_page(second, scope=scope)
    assert [item.target_id for item in fabric.targets(session.session_id)] == [first.target_id]
    assert {item.kind for item in fabric.operations(session.session_id)} >= {
        "new_page", "select_page", "close_page",
    }


@pytest.mark.asyncio
async def test_download_and_trace_payloads_are_cas_artifacts(browser_runtime):
    fabric, _adapters = browser_runtime
    scope = WorkScope(chat_id="chat-artifacts")
    session = await fabric.open_session(scope=scope)
    observation = await fabric.observe(fabric.page_ref(session.session_id), scope=scope)
    element = observation.one(name="Send")

    result = await fabric.click(element, expect_download=True, scope=scope)
    trace = await fabric.start_trace(session.session_id, scope=scope)
    trace = await fabric.stop_trace(trace.trace_id, scope=scope)

    assert result["download"]["artifact_ref"].startswith("artifact://sha256/")
    assert fabric.downloads(session.session_id)[0].suggested_filename == "report.bin"
    assert trace.state == "completed"
    assert trace.artifact_ref.startswith("artifact://sha256/")


@pytest.mark.asyncio
async def test_declared_oversized_download_is_cancelled_before_path_materialization(
    tmp_path,
):
    class Download:
        suggested_filename = "huge.bin"
        url = "https://download.test/huge"

        def __init__(self):
            self.cancelled = False
            self.path_calls = 0

        async def cancel(self):
            self.cancelled = True

        async def path(self):
            self.path_calls += 1
            raise AssertionError("oversized download path must not be materialized")

    class Response:
        url = "https://download.test/huge"
        headers = {"content-length": str(256 * 1024 * 1024 + 1)}

    item = Download()

    class Pending:
        value = None

        async def __aenter__(self):
            self.value = asyncio.sleep(0, result=item)
            return self

        async def __aexit__(self, *_args):
            return False

    class Keyboard:
        def __init__(self, page):
            self.page = page

        async def press(self, _keys):
            for callback in list(self.page.listeners):
                callback(Response())

    class Page:
        url = "https://download.test/page"

        def __init__(self):
            self.listeners = []
            self.keyboard = Keyboard(self)

        def on(self, _name, callback):
            self.listeners.append(callback)

        def remove_listener(self, _name, callback):
            self.listeners.remove(callback)

        def expect_download(self, **_kwargs):
            return Pending()

        async def title(self):
            return "Download"

    adapter = ManagedPlaywrightAdapter(str(tmp_path / "profile"))
    adapter._pages["page"] = Page()
    adapter._active_id = "page"

    with pytest.raises(BrowserValidationError, match="exceeds 256 MiB"):
        await adapter.perform(
            "page", "keys", {"keys": "Enter", "expect_download": True}
        )
    assert item.cancelled is True
    assert item.path_calls == 0


@pytest.mark.asyncio
async def test_oversized_trace_is_rejected_before_cas_ingestion(
    browser_runtime, monkeypatch,
):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-large-trace")
    session = await fabric.open_session(scope=scope)
    trace = await fabric.start_trace(session.session_id, scope=scope)

    async def oversized_trace(path):
        Path(path).write_bytes(b"x")
        with open(path, "r+b") as handle:
            handle.truncate(1024 * 1024 * 1024 + 1)

    adapters[0].stop_trace = oversized_trace
    monkeypatch.setattr(
        fabric.artifact_store,
        "put_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized trace must not enter CAS")
        ),
    )
    with pytest.raises(BrowserValidationError, match="exceeds 1 GiB"):
        await fabric.stop_trace(trace.trace_id, scope=scope)
    assert fabric.traces(session.session_id)[0].state == "failed"


@pytest.mark.asyncio
async def test_recovery_bumps_generation_and_old_page_handle_is_stale(browser_runtime):
    fabric, adapters = browser_runtime
    session = await fabric.open_session()
    page = fabric.page_ref(session.session_id)
    await adapters[0].close()
    fabric._adapters.pop(session.session_id)

    recovered = await fabric.recover_session(session.session_id)

    assert recovered.generation == session.generation + 1
    with pytest.raises(BrowserStaleReference, match="previous browser generation"):
        await fabric.observe(page)


@pytest.mark.asyncio
async def test_unimplemented_browser_kinds_are_not_persisted(tmp_path):
    fabric = create_browser_fabric(data_dir=str(tmp_path / "data"))

    with pytest.raises(BrowserValidationError, match="unknown browser kind"):
        await fabric.open_session(kind="chrome", profile_name="signed-in")

    assert fabric.sessions(include_closed=True) == ()
    assert set(fabric.capability_matrix()) == {"embedded", "managed"}


@pytest.mark.asyncio
async def test_events_are_monotonic_and_resumable(browser_runtime):
    fabric, _adapters = browser_runtime
    session = await fabric.open_session()
    page = fabric.page_ref(session.session_id)
    await fabric.observe(page)
    await fabric.screenshot(fabric.page_ref(session.session_id))

    events = fabric.events(session.session_id)
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    tail = fabric.events(session.session_id, after_sequence=sequences[-2])
    assert [event.sequence for event in tail] == sequences[-1:]


@pytest.mark.asyncio
async def test_host_read_still_records_the_session_authority_scope(browser_runtime):
    fabric, _adapters = browser_runtime
    owner = WorkScope(
        chat_id="chat-scope", workspace_id="workspace-scope",
        goal_id="goal-scope", step_id="step-scope",
    )
    session = await fabric.open_session(scope=owner)

    await fabric.observe(fabric.page_ref(session.session_id))

    assert fabric.operations(session.session_id)[0].scope == owner
    assert fabric.events(session.session_id)[-1].scope == owner


def test_expected_state_compare_and_set_is_typed(browser_runtime):
    fabric, _adapters = browser_runtime
    expected = BrowserExpectedState(session_generation=3, document_epoch=4)
    assert expected.to_dict() == {"session_generation": 3, "document_epoch": 4}


@pytest.mark.asyncio
async def test_startup_reconciles_dispatched_crash_window_without_replay(browser_runtime):
    fabric, adapters = browser_runtime
    session = await fabric.open_session()
    target = fabric.targets(session.session_id)[0]
    operation, _ = fabric.store.prepare_operation(
        session_id=session.session_id, target_id=target.target_id, kind="click",
        idempotency_key="crashed", request_fingerprint="a" * 64,
        generation=session.generation, document_epoch=target.document_epoch,
        observation_revision=target.observation_revision, scope=session.scope,
    )
    fabric.store.transition_operation(operation.operation_id, "dispatched")

    result = fabric.startup()

    assert result["operations_reconciled"]["unknown_effect"] == 1
    assert fabric.store.get_operation(operation.operation_id).state == "unknown_effect"
    assert adapters[0].perform_calls == []


@pytest.mark.asyncio
async def test_browser_capabilities_are_absorbed_into_seeds_and_returned_handles(
    browser_runtime, monkeypatch,
):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-cap", workspace_id="workspace-cap")
    context = capability_context(scope)
    registry = ToolRegistry()
    broker = FakeCapabilityBroker()
    composed = SimpleNamespace(
        browser=fabric, registry=registry, broker=broker
    )
    host = SimpleNamespace(
        require_runtime=lambda: composed, remote_handle_routers={},
    )
    monkeypatch.setattr(browser_capabilities, "_context", lambda: context)
    browser_capabilities.register_browser_fabric_tools(host)

    names = {
        "browser_profile_create", "browser_profile_list", "browser_session_list",
        "browser_open", "browser_observe", "browser_action", "browser_tab_list",
        "browser_tab_new", "browser_tab_select", "browser_tab_close",
        "browser_trace_start", "browser_trace_stop", "browser_close",
    }
    assert names.isdisjoint({tool.name for tool in composed.registry.all()})
    assert "browser" in host.remote_handle_routers

    session = await fabric.open_session(
        profile_name="cap-profile", scope=scope,
    )
    target = fabric.store.get_target(session.current_target_id)
    session_identity = session_handle_envelope(
        session, broker=composed.broker, context=context,
    )["$variant1_handle"]
    page_identity = page_handle_envelope(
        target.page_ref(session.generation), target=target,
        broker=composed.broker, context=context,
    )["$variant1_handle"]
    router = host.remote_handle_routers["browser"]

    observed = await router(
        context, page_identity, "observe", {"include_html": True}
    )
    pages = await router(context, session_identity, "pages", {})
    events = await router(
        context, session_identity, "history", {"kind": "events"},
    )

    assert observed["schema"] == "variant1.browser-observation-result.v1"
    assert observed["text"] == "Hello durable browser"
    assert observed["html"] == "<button>Send</button>"
    assert observed["html_ref"].startswith("artifact://sha256/")
    assert observed["html_truncated"] is False
    assert pages["current"]["$variant1_handle"]["id"] == target.target_id
    assert events
    assert adapters[0].observe_calls >= 1


@pytest.mark.asyncio
async def test_browser_remote_handle_router_reuses_elements_and_enforces_scope(
    browser_runtime, monkeypatch,
):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-router", goal_id="goal-router")
    context = capability_context(scope)
    registry = ToolRegistry()
    broker = FakeCapabilityBroker()
    composed = SimpleNamespace(
        browser=fabric, registry=registry, broker=broker
    )
    host = SimpleNamespace(
        require_runtime=lambda: composed, remote_handle_routers={},
    )
    monkeypatch.setattr(browser_capabilities, "_context", lambda: context)
    browser_capabilities.register_browser_fabric_tools(host)
    session = await fabric.open_session(scope=scope)
    target = fabric.targets(session.session_id)[0]
    session_identity = session_handle_envelope(
        session, broker=composed.broker, context=context,
    )["$variant1_handle"]
    page_identity = page_handle_envelope(
        target.page_ref(session.generation), target=target,
        broker=composed.broker, context=context,
    )["$variant1_handle"]
    router = host.remote_handle_routers["browser"]

    observed = await router(context, page_identity, "observe", {})
    element_identity = observed["elements"][0]["$variant1_handle"]
    newer = await router(
        context,
        observed["page"]["$variant1_handle"],
        "observe",
        {},
    )
    # Page handles are stable tab identities. The original handle transparently
    # resolves the latest target revision after later observations/actions.
    latest = await router(context, page_identity, "observe", {})
    clicked = await router(context, element_identity, "click", {})

    assert newer["snapshot"]["revision"] > observed["snapshot"]["revision"]
    assert latest["snapshot"]["revision"] > newer["snapshot"]["revision"]
    assert element_identity["metadata"]["backend_ref"] == "mf_aaaaaaaaaaaa_1"
    assert clicked["result"]["action"] == "click"
    clicked_with_options = await router(
        context,
        element_identity,
        "click",
        {"position": {"x": 12, "y": 3}, "force": True, "timeout": 2500},
    )
    assert clicked_with_options["result"]["action"] == "click"
    assert adapters[0].perform_calls[-1][2]["position"] == {"x": 12, "y": 3}
    assert adapters[0].perform_calls[-1][2]["force"] is True
    assert adapters[0].perform_calls[-1][2]["timeout_ms"] == 2500
    clicked_again = await router(context, element_identity, "click", {})
    assert clicked_again["result"]["action"] == "click"

    other = capability_context(WorkScope(chat_id="another-chat"))
    with pytest.raises(ToolError, match="outside the owning WorkScope"):
        await router(other, session_identity, "pages", {})


@pytest.mark.asyncio
async def test_managed_click_uses_a_short_default_interaction_timeout(tmp_path):
    class Locator:
        def __init__(self):
            self.timeout = None

        async def count(self):
            return 1

        async def click(self, **options):
            self.timeout = options["timeout"]
            self.options = options
            return None

    class Page:
        def __init__(self):
            self.target = Locator()

        def locator(self, _selector):
            return self.target

    adapter = ManagedPlaywrightAdapter(str(tmp_path / "profile-click-timeout"))
    page = Page()

    await adapter._perform_core(
        page,
        "click",
        {"backend_ref": "mf_aaaaaaaaaaaa_1"},
    )

    assert page.target.timeout == 4_000

    await adapter._perform_core(
        page,
        "click",
        {
            "backend_ref": "mf_aaaaaaaaaaaa_1",
            "timeout": 2500,
            "force": True,
            "position": {"x": 12, "y": 3},
        },
    )
    assert page.target.options == {
        "timeout": 2500,
        "force": True,
        "position": {"x": 12.0, "y": 3.0},
    }


@pytest.mark.asyncio
async def test_managed_action_reports_a_missing_stable_element_as_stale(tmp_path):
    class Locator:
        async def count(self):
            return 0

    class Page:
        def locator(self, _selector):
            return Locator()

    adapter = ManagedPlaywrightAdapter(str(tmp_path / "profile-missing-ref"))

    with pytest.raises(BrowserStaleReference, match="no longer exists"):
        await adapter._perform_core(
            Page(),
            "click",
            {"backend_ref": "mf_aaaaaaaaaaaa_1"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["embedded", "managed"])
async def test_common_browser_keys_reach_both_adapters_and_share_replay_identity(
    browser_runtime, kind,
):
    fabric, adapters = browser_runtime
    scope = WorkScope(chat_id="chat-key-aliases")
    try:
        session = await fabric.open_session(kind=kind, scope=scope)
        page = fabric.page_ref(session.session_id)
        first = await fabric.keys(page, "ARROWRIGHT", scope=scope, idempotency_key="arrow")
        replay = await fabric.keys(page, "ArrowRight", scope=scope, idempotency_key="arrow")
        assert replay == first
        calls = [row for row in adapters[0].perform_calls if row[1] == "keys"]
        assert len(calls) == 1
        assert calls[0][2]["keys"] == "ArrowRight"
        await fabric.keys(page, "ctrl++", scope=scope)
        assert adapters[0].perform_calls[-1][2]["keys"] == "Control++"
        before = len(adapters[0].perform_calls)
        with pytest.raises(BrowserValidationError):
            await fabric.keys(page, "Control+", scope=scope)
        assert len(adapters[0].perform_calls) == before
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_embedded_adapter_forwards_click_options_and_keys():
    commands = []

    async def request(command):
        commands.append(dict(command))
        return {
            "ok": True,
            "message": "done",
            "value": {"ready": True} if command["action"] == "evaluate" else None,
            "state": {"title": "Visible", "url": "https://example.test"},
        }

    adapter = EmbeddedBrowserAdapter(request=request)
    adapter._target_ids.add("electron_visible")
    adapter._active_id = "electron_visible"
    await adapter.perform(
        "electron_visible",
        "click",
        {
            "backend_ref": "b2-4",
            "position": {"x": 12, "y": 3},
            "force": True,
            "timeout_ms": 2500,
        },
    )
    await adapter.perform(
        "electron_visible",
        "keys",
        {"backend_ref": "b2-4", "keys": "ArrowRight"},
    )
    evaluated = await adapter.perform(
        "electron_visible",
        "evaluate",
        {"expression": "arg => ({value: arg})", "arg": 7},
    )

    assert commands[0] == {
        "action": "click",
        "owner_chat_id": "",
        "tab_id": "electron_visible",
        "target": "b2-4",
        "position": {"x": 12, "y": 3},
        "force": True,
        "timeout_ms": 2500,
    }
    assert commands[1] == {
        "action": "keys",
        "owner_chat_id": "",
        "tab_id": "electron_visible",
        "keys": "ArrowRight",
        "target": "b2-4",
    }
    assert commands[2] == {
        "action": "evaluate",
        "owner_chat_id": "",
        "tab_id": "electron_visible",
        "expression": "arg => ({value: arg})",
        "arg": 7,
    }
    assert evaluated.value == {"ready": True}


@pytest.mark.asyncio
async def test_embedded_stale_guest_error_is_a_retryable_stale_reference():
    async def request(command):
        if command["action"] == "click":
            raise RuntimeError("element reference is stale; run browser_read again")
        return {"ok": True, "downloads": [], "state": {"url": "https://example.test"}}

    adapter = EmbeddedBrowserAdapter(request=request)
    adapter._target_ids.add("electron_visible")
    adapter._active_id = "electron_visible"
    with pytest.raises(BrowserStaleReference, match="element reference is stale"):
        await adapter.perform(
            "electron_visible", "click", {"backend_ref": "b2-4"}
        )


@pytest.mark.asyncio
async def test_embedded_noop_back_does_not_claim_navigation():
    async def request(command):
        if command["action"] == "back":
            return {
                "ok": True, "navigated": False,
                "state": {"url": "https://example.test"},
            }
        return {"ok": True, "downloads": [], "state": {"url": "https://example.test"}}

    adapter = EmbeddedBrowserAdapter(request=request)
    adapter._target_ids.add("electron_visible")
    adapter._active_id = "electron_visible"
    adapter._states["electron_visible"] = {"url": "https://example.test"}
    result = await adapter.perform("electron_visible", "back", {})
    assert result.navigated is False


@pytest.mark.asyncio
async def test_staged_native_download_commits_before_ack_and_survives_lost_ack(browser_runtime, tmp_path):
    import hashlib
    from work_fabric.scope import bind_work_scope

    fabric, _ = browser_runtime
    owner = WorkScope(chat_id="download-owner")
    session = await fabric.open_session(kind="embedded", headless=False, scope=owner)
    target = fabric.store.list_targets(session.session_id)[0]
    staging = Path(fabric.download_staging_root)
    staging.mkdir(parents=True, exist_ok=True)
    payload = bytes(range(256)) * 8192
    path = staging / "native-download.bin"
    path.write_bytes(payload)
    raw = {"download_id": "native-1", "status": "completed", "path": str(path),
           "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
           "suggested_filename": "report.bin", "url": "http://localhost/report"}
    ack_attempts = 0

    async def request(command):
        nonlocal ack_attempts
        if command["action"] == "drain_downloads":
            return {"downloads": [dict(raw)]}
        if command["action"] == "ack_downloads":
            assert command["download_ids"] == ["native-1"]
            record = fabric.store.list_downloads(session.session_id)[0]
            assert fabric.artifact_store.read_bytes_scoped(record.artifact_ref, owner.chat_id) == payload
            ack_attempts += 1
            if ack_attempts == 1:
                raise RuntimeError("lost acknowledgement")
            path.unlink()
        return {"ok": True}

    adapter = EmbeddedBrowserAdapter(request=request)
    adapter.set_download_sink(lambda backend_id, operation_id, download:
        fabric._record_adapter_download(session.session_id, backend_id, operation_id, download))
    with bind_work_scope(owner):
        with pytest.raises(BrowserUnavailable, match="lost acknowledgement"):
            await adapter._drain_downloads("", target.backend_target_id)
        assert path.exists()
    fabric._adapters[session.session_id] = adapter
    with bind_work_scope(WorkScope(chat_id="other-chat")):
        await fabric.refresh_downloads(session.session_id)
    rows = fabric.store.list_downloads(session.session_id)
    assert len(rows) == 1 and rows[0].scope == owner
    assert len([e for e in fabric.store.events(session.session_id) if e.kind == "download.completed"]) == 1
    assert not path.exists() and ack_attempts == 2
    with pytest.raises(PermissionError):
        fabric.artifact_store.read_bytes_scoped(rows[0].artifact_ref, "other-chat")

    outside = tmp_path / "unowned.bin"
    outside.write_bytes(payload)
    raw.update(download_id="native-2", path=str(outside))
    with bind_work_scope(owner):
        await adapter._drain_downloads("", target.backend_target_id)
        raw["path"] = str(staging / "tampered.bin")
        Path(raw["path"]).write_bytes(b"different content")
        await adapter._drain_downloads("", target.backend_target_id)
    assert ack_attempts == 2 and len(fabric.store.list_downloads(session.session_id)) == 1


@pytest.mark.asyncio
async def test_attachment_navigation_keeps_committed_document_epoch():
    async def request(command):
        assert command["operation_id"] == "browserop_test"
        return {"ok": True, "navigated": False, "download_started": True,
                "state": {"url": "http://localhost/original"}}
    adapter = EmbeddedBrowserAdapter(request=request)
    adapter._target_ids.add("electron_visible")
    adapter._states["electron_visible"] = {"url": "http://localhost/original"}
    result = await adapter.perform("electron_visible", "navigate",
                                   {"url": "http://localhost/report", "_operation_id": "browserop_test"})
    assert not result.navigated and result.url == "http://localhost/original"
    assert result.value["download_started"]


@pytest.mark.asyncio
async def test_managed_launch_rebinds_marked_pages_not_context_order(tmp_path):
    class Page:
        def __init__(self, url, marker):
            self.url = url
            self.marker = marker

        async def evaluate(self, expression, value=None):
            if value is None:
                return self.marker
            self.marker = value
            return value

        async def title(self):
            return self.url.rsplit("/", 1)[-1]

        def is_closed(self):
            return False

    page_a = Page("https://example.test/a", "__variant1_fabric_target__:target-a")
    page_b = Page("https://example.test/b", "__variant1_fabric_target__:target-b")

    class Context:
        pages = [page_b, page_a]  # deliberately opposite durable creation order

        def on(self, _event, _callback):
            pass

        async def new_page(self):
            return Page("about:blank", "")

    class Chromium:
        async def launch_persistent_context(self, _profile, **_options):
            return Context()

    runtime = SimpleNamespace(chromium=Chromium())
    adapter = ManagedPlaywrightAdapter(
        str(tmp_path / "marked-profile"),
        playwright_factory=lambda: runtime,
    )
    adapter.set_current_target_id("target-b")
    targets = [
        SimpleNamespace(
            backend_target_id="target-a", url=page_a.url,
            state="active",
        ),
        SimpleNamespace(
            backend_target_id="target-b", url=page_b.url,
            state="active",
        ),
        SimpleNamespace(
            backend_target_id="orphan", url="https://wrong.test/",
            state="orphaned",
        ),
    ]

    await adapter.launch(targets)

    assert adapter._pages["target-a"] is page_a
    assert adapter._pages["target-b"] is page_b
    assert "orphan" not in adapter._pages
    assert adapter._active_id == "target-b"


@pytest.mark.asyncio
async def test_embedded_observation_forwards_the_requested_element_bound():
    commands = []

    async def request(command):
        commands.append(dict(command))
        return {
            "ok": True,
            "title": "Visible",
            "url": "https://example.test",
            "text": "Ready",
            "elements": [],
            "state": {"title": "Visible", "url": "https://example.test"},
        }

    adapter = EmbeddedBrowserAdapter(request=request)
    adapter._target_ids.add("electron_visible")
    adapter._active_id = "electron_visible"

    await adapter.observe(
        "electron_visible",
        max_chars=1_000,
        max_elements=275,
        include_html=False,
        include_screenshot=False,
    )

    assert commands == [{
        "action": "read", "tab_id": "electron_visible",
        "max_elements": 275, "max_chars": 1_000,
        "owner_chat_id": "",
    }]


@pytest.mark.asyncio
async def test_embedded_adapter_owns_explicit_visible_workbench_tabs():
    commands = []

    async def request(command):
        commands.append(dict(command))
        tab_id = str(command.get("tab_id") or "")
        if command["action"] == "tabs":
            return {
                "ok": True,
                "tabs": [
                    {"id": "page-a", "title": "A", "url": "https://a.test", "active": False},
                    {"id": "page-b", "title": "B", "url": "https://b.test", "active": True},
                ],
                "state": {"tab_id": "page-b", "url": "https://b.test"},
            }
        return {
            "ok": True,
            "target": {"id": tab_id, "url": command.get("url") or "about:blank"},
            "state": {"tab_id": tab_id, "url": command.get("url") or "about:blank"},
        }

    adapter = EmbeddedBrowserAdapter(request=request)
    launched = await adapter.launch((
        SimpleNamespace(backend_target_id="page-a", url="https://a.test"),
        SimpleNamespace(backend_target_id="page-b", url="https://b.test"),
    ))

    assert [item.backend_target_id for item in launched] == ["page-a", "page-b"]
    assert launched[1].active is True
    assert commands == [{"action": "tabs", "owner_chat_id": ""}]
    await adapter.activate_page("page-a")
    await adapter.close_page("page-b")
    assert commands[-2]["action"] == "activate_page"
    assert commands[-1]["action"] == "close_page"


@pytest.mark.asyncio
async def test_browser_terminal_operation_and_event_roll_back_together(
    browser_runtime,
):
    fabric, _adapters = browser_runtime
    scope = WorkScope(chat_id="chat-atomic-browser")
    session = await fabric.open_session(scope=scope)
    target = fabric.targets(session.session_id)[0]
    request = {"kind": "click", "target_id": target.target_id}
    operation, replay = fabric.store.prepare_operation(
        session_id=session.session_id,
        target_id=target.target_id,
        kind="click",
        idempotency_key="atomic-click",
        request_fingerprint=request_fingerprint("browser.click", request),
        generation=session.generation,
        document_epoch=target.document_epoch,
        observation_revision=target.observation_revision,
        scope=scope,
    )
    assert replay is False
    fabric.store.transition_operation(operation.operation_id, "dispatched")
    before = len([
        event for event in fabric.events(session.session_id)
        if event.kind == "operation.failed"
    ])

    with inject_faults("browser.before_commit"):
        with pytest.raises(InjectedFault, match="browser.before_commit"):
            fabric.store.transition_operation(
                operation.operation_id, "failed", diagnostic="injected"
            )

    assert fabric.store.get_operation(operation.operation_id).state == "dispatched"
    after = len([
        event for event in fabric.events(session.session_id)
        if event.kind == "operation.failed"
    ])
    assert after == before
