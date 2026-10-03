"""The user browser shares the agent reader's frozen scope and export path."""
import json
from types import SimpleNamespace

import pytest

from artifacts.store import ContentAddressedArtifactStore
from external_context import SessionContextService
from tests.support.conversation_sessions import open_sessions
import ws_context_browser


@pytest.fixture
def setup(tmp_path):
    sessions = open_sessions(tmp_path / "sessions", max_messages=1)
    chat = sessions.create_session()
    other = sessions.create_session()
    sessions.append_messages(chat, [{"role": "user", "text": "Older Straße record"}, {"role": "assistant", "text": "Later correction"}])
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    kernel = SimpleNamespace(cell_ledger=SimpleNamespace(tail=lambda *args, **kwargs: []))
    service = SessionContextService(database_path=str(tmp_path / "context.sqlite3"), sessions=sessions,
        kernel=kernel, artifacts=artifacts, runtimes=SimpleNamespace(snapshot_store=None))
    runtime = SimpleNamespace(sessions=sessions, session_context=service)
    return runtime, chat, other


@pytest.mark.asyncio
async def test_selected_chat_browser_freezes_pages_and_rejects_foreign_view(setup, tmp_path):
    runtime, chat, other = setup
    session = SimpleNamespace(viewed_session_id=other)
    view = await ws_context_browser.operate(runtime, session, {"operation": "capture", "chat_id": chat})
    view_id = view["view_id"]
    saved = await ws_context_browser.operate(runtime, session, {"operation": "views", "chat_id": chat})
    assert saved["items"][0]["view_id"] == view_id
    request = {"chat_id": chat, "view_id": view_id}
    read = await ws_context_browser.operate(runtime, session, {**request, "operation": "read", "limit": 1})
    assert read["has_more"] and read["items"][0]["preview"] == "Older Straße record"
    search = await ws_context_browser.operate(runtime, session, {**request, "operation": "search", "query": "STRASSE"})
    assert search["items"][0]["source_id"] == read["items"][0]["source_id"]
    runtime.sessions.append_messages(chat, [{"role": "user", "text": "Newest evidence"}])
    assert len((await ws_context_browser.operate(runtime, session, {**request, "operation": "read"}))["items"]) == 2
    refreshed = await ws_context_browser.operate(runtime, session, {**request, "operation": "refresh"})
    assert refreshed["status"]["counts"]["message"] == 3
    with pytest.raises(Exception, match="scope|unavailable"):
        await ws_context_browser.operate(runtime, session, {"operation": "read", "chat_id": other, "view_id": view_id})
    target = tmp_path / "frozen.jsonl"
    exported = await ws_context_browser.operate(runtime, session, {**request, "operation": "export", "path": str(target)})
    assert exported["source_count"] == 2
    assert "Newest evidence" not in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_rejection_is_correlated_and_does_not_create_unknown_chat(setup):
    runtime, chat, other = setup
    handlers = {}
    def on(name):
        def decorate(fn):
            handlers[name] = fn
            return fn
        return decorate
    ws_context_browser.register(on)
    sent = []
    async def send_json(value):
        sent.append(value)
    await handlers["external-context:request"](SimpleNamespace(require_runtime=lambda: runtime),
        SimpleNamespace(send_json=send_json), SimpleNamespace(viewed_session_id=chat),
        {"operation": "capture", "chat_id": "absent", "request_id": "capture-one"})
    assert sent[0]["ok"] is False and sent[0]["request_id"] == "capture-one"
    assert not runtime.sessions.has_session("absent")
    with pytest.raises(ValueError, match="limit"):
        await ws_context_browser.operate(runtime, None, {"operation": "views", "chat_id": chat, "limit": True})


@pytest.mark.asyncio
async def test_child_source_roster_is_bounded_and_excludes_deleted(setup):
    runtime, chat, _ = setup
    def snapshot(parent, *, limit):
        assert parent == chat and limit == 100
        return {"children": [{"child_id": "goal-worker", "name": "Goal analysis", "status": "completed", "deletion_state": "active"},
                             {"child_id": "deleted-worker", "deletion_state": "deleted"}], "truncated": True}
    runtime.catalog = SimpleNamespace(children=SimpleNamespace(inspection_snapshot=snapshot))
    roster = await ws_context_browser.operate(runtime, None, {"operation": "children", "chat_id": chat})
    assert roster["truncated"] is True
    assert [row["child_id"] for row in roster["items"]] == ["goal-worker"]
