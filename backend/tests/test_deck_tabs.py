"""Deck tabs follow their runs; user mentions resolve only to the exact tab."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from browser_fabric.adapters import EmbeddedBrowserAdapter
from browser_fabric.deck_tabs import cleanup_run_tabs, resolve_tab_references
from run_context import Variant1RunContext, bind_run_context
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository


def _tabs(*rows):
    async def request(command, timeout=0):
        assert command == {"action": "tabs", "owner_chat_id": "chat-a"}
        return {"tabs": list(rows)}
    return request


def _ref(**overrides):
    row = {"kind": "browser_tab", "tab_id": "t1", "owner_chat_id": "chat-a",
           "title": "Docs", "url": "https://example.test/docs"}
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_a_mentioned_tab_resolves_only_to_that_unchanged_tab():
    live = {"id": "t1", "title": "Docs", "url": "https://example.test/docs"}
    note = await resolve_tab_references([_ref()], "chat-a", request=_tabs(live))
    assert note.startswith("[Mentioned browser tab t1: Docs]\nURL: https://example.test/docs\n")
    assert "https://example.test/docs" in note and "unavailable" not in note

    changed = {**live, "title": "Other page"}
    note = await resolve_tab_references([_ref()], "chat-a", request=_tabs(changed))
    assert note.startswith("[Mentioned browser tab t1 is unavailable: Docs]")
    assert "Do not use or open a different tab" in note

    closed = await resolve_tab_references([_ref()], "chat-a", request=_tabs())
    assert "is unavailable" in closed
    foreign = await resolve_tab_references([_ref(owner_chat_id="chat-b")], "chat-a",
                                           request=_tabs(live))
    assert "is unavailable" in foreign


@pytest.mark.asyncio
async def test_mentions_fail_closed_when_the_deck_cannot_answer():
    async def broken(_command, timeout=0):
        raise RuntimeError("host disconnected")
    note = await resolve_tab_references([_ref()], "chat-a", request=broken)
    assert "is unavailable" in note


@pytest.mark.asyncio
async def test_only_browser_tab_references_count_and_they_are_bounded():
    assert await resolve_tab_references([{"kind": "file"}], "chat-a", request=_tabs()) == ""
    assert await resolve_tab_references("not-a-list", "chat-a", request=_tabs()) == ""
    many = [_ref(tab_id=f"t{i}") for i in range(20)]
    note = await resolve_tab_references(many, "chat-a", request=_tabs())
    assert note.count("[Mentioned browser tab") == 8


@pytest.mark.asyncio
async def test_run_cleanup_asks_the_deck_and_tolerates_a_closed_deck():
    request = AsyncMock(return_value={"ok": True})
    await cleanup_run_tabs("chat-a", "run-1", request=request)
    assert request.await_args.args[0] == {
        "action": "cleanup_run", "owner_chat_id": "chat-a", "run_id": "run-1",
    }
    await cleanup_run_tabs("chat-a", "run-2", request=AsyncMock(side_effect=RuntimeError("closed")))
    silent = AsyncMock()
    await cleanup_run_tabs("chat-a", "", request=silent)
    silent.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_commands_name_their_run_and_marks_reach_the_deck():
    sent = []

    async def request(command):
        sent.append(dict(command))
        return {"ok": True}

    adapter = EmbeddedBrowserAdapter(request=request, owner_chat_id="chat-a")
    await adapter._call("tabs")
    assert "run_id" not in sent[-1]
    context = Variant1RunContext.create(source="chat", session_id="chat-a")
    with bind_run_context(context):
        await adapter.mark_page("t1", "deliverable")
        marked = dict(sent[-1])
        await adapter.show_page("t1")
    assert marked == {"action": "mark_page", "tab_id": "t1", "mark": "deliverable",
                      "owner_chat_id": "chat-a", "run_id": context.run_id}
    assert sent[-1] == {"action": "activate_page", "tab_id": "t1", "visible": True,
                        "owner_chat_id": "chat-a", "run_id": context.run_id}


def test_registry_lists_the_runs_admitted_right_now(tmp_path):
    runtimes = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3")))
    assert runtimes.active_runs() == []
    admission = runtimes.try_reserve_run("chat-a")
    runtimes.begin_run(admission, run_id="run-1", thread_id="run-1")
    assert runtimes.active_runs() == [{"chat_id": "chat-a", "run_id": "run-1", "admission_id": admission}]
    runtimes.finish_run(admission, status="ok")
    assert runtimes.active_runs() == []


@pytest.mark.asyncio
async def test_deck_registration_receives_the_active_runs(monkeypatch):
    import ws_browser
    import ws_dispatch
    from browser_fabric import interactive

    socket = SimpleNamespace(send_json=AsyncMock())
    runtimes = SimpleNamespace(active_runs=lambda: [
        {"chat_id": "chat-a", "run_id": "run-1", "admission_id": "adm"},
    ])
    server = SimpleNamespace(require_runtime=lambda: SimpleNamespace(session_runtimes=runtimes))
    assert ws_browser  # registers the handler
    try:
        await ws_dispatch.HANDLERS["browser:host:register"](server, socket, None, {})
    finally:
        interactive.unregister_host(socket)
    reply = socket.send_json.await_args.args[0]
    assert reply["type"] == "browser:host:registered"
    assert reply["active_runs"] == [{"owner_chat_id": "chat-a", "run_id": "run-1"}]


@pytest.mark.asyncio
async def test_chat_send_puts_resolved_mentions_in_the_model_context(tmp_path, monkeypatch):
    import ws_dispatch
    from tests.test_ws_dispatch_chat import _chat_stack

    seen = {}

    async def run_task(*_args, **kwargs):
        seen.update(kwargs)

    async def resolve(references, owner):
        seen["owner"] = owner
        return "[Mentioned browser tab t1: Docs]\nURL: https://example.test/docs"

    monkeypatch.setattr("browser_fabric.deck_tabs.resolve_tab_references", resolve)
    srv, _runtimes, _repository, session, sid = _chat_stack(tmp_path, run_task)
    await ws_dispatch.HANDLERS["chat"](srv, AsyncMock(), session, {
        "type": "chat", "session_id": sid, "text": "summarize this tab",
        "references": [_ref(owner_chat_id=sid)],
    })
    await session.active.turn_task
    assert seen["owner"] == sid
    assert "[Mentioned browser tab t1: Docs]" in seen["attachment_text"]


def test_tab_notes_round_trip_into_transcript_chips():
    from tab_mentions import tab_mention_chips, tab_mention_note

    notes = "\n\n".join([
        tab_mention_note("t1", "Docs [beta]\nv2", "https://example.test/a]b", available=True),
        tab_mention_note("t2", "", "https://gone.test/", available=False),
        tab_mention_note("t1", "Docs", "https://example.test/", available=True),
    ])
    assert tab_mention_chips("[Attached file: a.txt]\n\n" + notes) == [
        {"kind": "browser_tab", "name": "Docs [beta] v2", "tab_id": "t1",
         "title": "Docs [beta] v2", "url": "https://example.test/a]b"},
        {"kind": "browser_tab", "name": "https://gone.test/", "tab_id": "t2",
         "title": "", "url": "https://gone.test/"},
    ]


def test_a_mention_only_message_shows_its_chip_and_no_text():
    from chat_attachments import display_user_message
    from chat_turn_plan import build_attachment_plan
    from tab_mentions import tab_mention_note

    note = tab_mention_note("t1", "Docs", "https://example.test/docs", available=True)
    chip = {"kind": "browser_tab", "name": "Docs", "tab_id": "t1",
            "title": "Docs", "url": "https://example.test/docs"}
    plan = build_attachment_plan(text="", attachment_text=note,
                                 display_user_message=display_user_message)
    assert plan.display_text == ""
    assert plan.display_attachments == (chip,)
    assert plan.model_text == note
    plan = build_attachment_plan(text="read this", attachment_text="\n\n" + note,
                                 display_user_message=display_user_message)
    assert plan.display_text == "read this"
    assert plan.display_attachments == (chip,)


def test_history_rows_keep_mentioned_tabs_as_references(tmp_path):
    from chat_sessions import build_chat_sessions

    path = str(tmp_path / "conversations.sqlite3")
    service = build_chat_sessions(path=path)
    sid = service.create_session("Mentions")
    chip = {"kind": "browser_tab", "name": "Docs", "tab_id": "t1",
            "title": "Docs", "url": "https://example.test/docs"}
    service.append_messages(sid, [
        {"role": "user", "text": "", "attachments": [chip]},
        {"role": "assistant", "text": "That page is about docs."},
        {"role": "user", "text": "and this", "attachments": [chip, {"name": "a.txt", "kind": "text"}]},
    ])
    messages = build_chat_sessions(path=path).get_session(sid)["messages"]
    reference = [{"tab_id": "t1", "title": "Docs", "url": "https://example.test/docs"}]
    assert [m["text"] for m in messages] == ["", "That page is about docs.", "and this"]
    assert messages[0]["references"] == reference and "attachments" not in messages[0]
    assert messages[2]["references"] == reference
    assert messages[2]["attachments"] == [{"name": "a.txt", "kind": "text"}]
