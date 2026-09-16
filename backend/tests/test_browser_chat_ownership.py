"""Real embedded adapter/store regressions for chat-owned live tab reconciliation."""
import asyncio
from contextlib import contextmanager

import pytest

from browser_fabric import (
    BrowserBinding, BrowserNotFound, BrowserScopeMismatch, BrowserStaleReference, BrowserUnavailable,
    bind_browser_binding, bind_browser_fabric, create_browser_fabric,
)
from work_fabric.scope import WorkScope
import tools_web as tools
from run_context import Variant1RunContext, bind_run_context


class Deck:
    def __init__(self):
        self.tabs = {}
        self.commands = []
        self.inventory_error = False

    def add(self, owner, identity, url="https://example.test/", active=True):
        self.tabs[identity] = {"id": identity, "owner_chat_id": owner,
                               "url": url, "title": identity, "active": active}

    async def request(self, command):
        self.commands.append(dict(command))
        owner = command["owner_chat_id"]  # Every RPC must carry its durable owner.
        action = command["action"]
        owned = lambda: [dict(t) for t in self.tabs.values() if t["owner_chat_id"] == owner]
        if action == "tabs":
            if self.inventory_error:
                raise RuntimeError("host unavailable")
            return {"ok": True, "tabs": owned()}
        target = command.get("tab_id", "")
        if action == "new_page":
            if target in self.tabs and self.tabs[target]["owner_chat_id"] != owner:
                raise RuntimeError("TAB_OWNER_MISMATCH")
            if target not in self.tabs:
                self.add(owner, target, command.get("url", "about:blank"))
            return {"ok": True, "target": dict(self.tabs[target]), "tabs": owned()}
        if action == "close_page":
            if target in self.tabs and self.tabs[target]["owner_chat_id"] == owner:
                del self.tabs[target]
            return {"ok": True, "tabs": owned()}
        if target not in self.tabs or self.tabs[target]["owner_chat_id"] != owner:
            raise RuntimeError("TAB_NOT_FOUND")
        row = self.tabs[target]
        state = {**row, "tab_id": target}
        if action == "navigate":
            row["url"] = command["url"]
            state["url"] = command["url"]
        if action in {"downloads", "drain_downloads"}:
            return {"ok": True, "downloads": [], "state": state}
        if action == "read":
            return {"ok": True, "state": state, "text": "Visible owned page", "elements": []}
        if action == "evaluate":
            return {"ok": True, "state": state, "value": {"document_id": target, "url": row["url"]}}
        return {"ok": True, "state": state}


def setup(tmp_path, deck):
    fabric = create_browser_fabric(data_dir=str(tmp_path), embedded_request=deck.request)
    binding = BrowserBinding(owner_kind="chat", owner_id="chat-a", scope=WorkScope(chat_id="chat-a"))
    return fabric, binding


@contextmanager
def chat_context(fabric, binding):
    context = Variant1RunContext.create(source="chat", work_scope=binding.scope, chat_session=object())
    with bind_run_context(context), bind_browser_fabric(fabric), bind_browser_binding(binding):
        yield


@pytest.mark.asyncio
async def test_missing_current_rebinds_owned_visible_tab_without_duplicate(tmp_path):
    deck = Deck()
    fabric, binding = setup(tmp_path, deck)
    try:
        with chat_context(fabric, binding):
            await tools.browser_navigate({"url": "https://first.test/", "kind": "embedded"})
            old = fabric.page_ref(binding.fabric_session_id)
            deck.tabs.clear()
            deck.add("chat-a", "url:user-opened", "https://user.test/")
            deck.add("chat-b", "url:foreign", "https://foreign.test/")
            start = len(deck.commands)
            result = await tools.browser_read({})
            assert "https://user.test/" in result
            assert not any(c["action"] == "new_page" for c in deck.commands[start:])
            session = fabric.session(binding.fabric_session_id)
            assert fabric.store.get_target(session.current_target_id).backend_target_id == "url:user-opened"
            assert fabric.store.get_target(old.target_id).state == "closed"
            with pytest.raises(BrowserNotFound):
                await fabric.observe(old, scope=binding.scope)
            assert deck.tabs["url:foreign"]["url"] == "https://foreign.test/"
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_empty_owned_inventory_creates_one_fresh_page_and_does_not_adopt_foreign(tmp_path):
    deck = Deck()
    fabric, binding = setup(tmp_path, deck)
    try:
        scope = binding.scope
        session = await fabric.open_session(kind="embedded", scope=scope)
        old = fabric.page_ref(session.session_id)
        deck.tabs.clear()
        deck.add("chat-b", "foreign")
        start = len(deck.commands)
        await asyncio.gather(*(fabric.reconcile_current_page(session.session_id, scope=scope) for _ in range(2)))
        assert sum(c["action"] == "new_page" for c in deck.commands[start:]) == 1
        assert len([t for t in deck.tabs.values() if t["owner_chat_id"] == "chat-a"]) == 1
        assert "foreign" in deck.tabs
        assert fabric.page_ref(session.session_id).target_id != old.target_id
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_explicit_missing_page_fails_without_retarget_or_replay(tmp_path):
    deck = Deck()
    fabric, binding = setup(tmp_path, deck)
    try:
        session = await fabric.open_session(kind="embedded", scope=binding.scope)
        page = fabric.page_ref(session.session_id)
        deck.tabs.clear()
        deck.add("chat-a", "replacement")
        start = len(deck.commands)
        with pytest.raises(BrowserUnavailable, match="TAB_NOT_FOUND"):
            await fabric.observe(page, scope=binding.scope)
        assert all(c.get("tab_id") != "replacement" for c in deck.commands[start:])
        assert not any(c["action"] == "new_page" for c in deck.commands[start:])
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_reconnect_adopts_owned_inventory_without_restoring_old_ids(tmp_path):
    deck = Deck()
    first, binding = setup(tmp_path, deck)
    session = await first.open_session(kind="embedded", scope=binding.scope)
    await first.shutdown()
    deck.tabs.clear()
    deck.add("chat-a", "after-remount")
    second, _ = setup(tmp_path, deck)
    try:
        start = len(deck.commands)
        recovered = await second.acquire_session(session.session_id, scope=binding.scope)
        assert recovered.generation > session.generation
        assert second.store.get_target(recovered.current_target_id).backend_target_id == "after-remount"
        assert not any(c["action"] == "new_page" for c in deck.commands[start:])
    finally:
        await second.shutdown()


@pytest.mark.asyncio
async def test_chats_keep_separate_sessions_current_pages_and_delete_only_owned_tabs(tmp_path):
    deck = Deck()
    fabric, binding = setup(tmp_path, deck)
    try:
        a = await fabric.open_session(kind="embedded", scope=binding.scope)
        b = await fabric.open_session(kind="embedded", scope=WorkScope(chat_id="chat-b"))
        again = await fabric.open_session(kind="embedded", scope=binding.scope.with_updates(kernel_generation=3))
        assert a.session_id == again.session_id != b.session_id
        assert a.profile_id == b.profile_id
        with pytest.raises(BrowserScopeMismatch):
            await fabric.acquire_session(b.session_id, scope=binding.scope)
        await fabric.delete_chat("chat-a")
        assert all(t["owner_chat_id"] == "chat-b" for t in deck.tabs.values())
        assert fabric.session(b.session_id).state == "active"
        assert fabric.session(a.session_id).state == "closed"
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_legacy_host_binding_does_not_transfer_tabs_into_new_chat(tmp_path):
    deck = Deck()
    fabric, binding = setup(tmp_path, deck)
    try:
        legacy = await fabric.open_session(kind="embedded")
        binding.fabric_session_id = legacy.session_id
        with chat_context(fabric, binding):
            _, owned = await tools._ensure_session({"kind": "embedded"})
        assert owned.session_id != legacy.session_id
        assert fabric.session(legacy.session_id).state == "active"
        assert len(deck.tabs) == 2
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_failed_inventory_keeps_durable_targets_and_never_creates_page(tmp_path):
    deck = Deck()
    fabric, binding = setup(tmp_path, deck)
    try:
        session = await fabric.open_session(kind="embedded", scope=binding.scope)
        deck.inventory_error = True
        start = len(deck.commands)
        with pytest.raises(BrowserUnavailable, match="host unavailable"):
            await fabric.reconcile_current_page(session.session_id, scope=binding.scope)
        assert fabric.page_ref(session.session_id).target_id == session.current_target_id
        assert [c["action"] for c in deck.commands[start:]] == ["tabs"]
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_live_current_page_survives_other_owned_tab_selection(tmp_path):
    deck = Deck()
    fabric, binding = setup(tmp_path, deck)
    try:
        session = await fabric.open_session(kind="embedded", scope=binding.scope)
        for row in deck.tabs.values():
            row["active"] = False
        deck.add("chat-a", "selected-by-user")
        current = await fabric.reconcile_current_page(session.session_id, scope=binding.scope)
        assert current.current_target_id == session.current_target_id
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_reappearing_owned_tab_reuses_identity_with_new_document_epoch(tmp_path):
    deck = Deck()
    fabric, binding = setup(tmp_path, deck)
    try:
        session = await fabric.open_session(kind="embedded", scope=binding.scope)
        original = fabric.page_ref(session.session_id)
        backend_id = fabric.store.get_target(original.target_id).backend_target_id
        deck.tabs.clear()
        empty = await fabric.reconcile_current_page(session.session_id, scope=binding.scope, create_if_empty=False)
        assert empty.current_target_id == ""
        deck.add("chat-a", backend_id, "https://restored.test/")
        restored = await fabric.reconcile_current_page(session.session_id, scope=binding.scope)
        assert restored.current_target_id == original.target_id
        assert len(fabric.targets(session.session_id, include_closed=True)) == 1
        with pytest.raises(BrowserStaleReference):
            await fabric.observe(original, scope=binding.scope)
    finally:
        await fabric.shutdown()


@pytest.mark.asyncio
async def test_connection_check_clears_missing_tab_notice_without_creating_or_replaying(tmp_path):
    deck = Deck()
    first, binding = setup(tmp_path, deck)
    session = await first.open_session(kind="embedded", scope=binding.scope)
    await first.preferences.connection_failed("chat-a", session, BrowserUnavailable("TAB_NOT_FOUND"))
    await first.shutdown()
    deck.tabs.clear()
    deck.add("chat-b", "foreign")
    second, _ = setup(tmp_path, deck)
    try:
        start = len(deck.commands)
        state = await second.preferences.refresh_state("chat-a")
        assert state["state"] == "idle"
        assert "TAB_NOT_FOUND" not in state["message"]
        assert [c["action"] for c in deck.commands[start:]] == ["tabs"]
        deck.add("chat-a", "opened-by-user")
        state = await second.preferences.refresh_state("chat-a")
        assert state["state"] == "ready"
        assert all(c["action"] == "tabs" for c in deck.commands[start:])
        assert set(deck.tabs) == {"foreign", "opened-by-user"}
    finally:
        await second.shutdown()
