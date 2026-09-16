"""Canonical memory extraction, approval, recall, and prompt boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chat_memory import MemoryChatRoute, parse_remember_command
from chat_pipeline import append_dynamic_memory_context
from memory_tools import MemoryPorts, extract_and_store, remember_explicit, retrieve_memory
from memory_store import MemoryStore
from tests.support.conversation_sessions import open_sessions


def _ports(tmp_path, *, router=None):
    return MemoryPorts(
        store=MemoryStore(str(tmp_path / "memory.sqlite3")),
        sessions=open_sessions(tmp_path / "chats"),
        router=router,
        hub=SimpleNamespace(broadcast=AsyncMock()),
        engine_status_msg=lambda: {"type": "status"},
    )


def test_parse_remember_is_explicit_only():
    assert parse_remember_command("/remember the codename is Cedar") == "the codename is Cedar"
    assert parse_remember_command(" /REMEMBER   concise bullets ") == "concise bullets"
    assert parse_remember_command("remember this") is None


@pytest.mark.asyncio
async def test_remember_route_bypasses_model_and_extraction():
    writer = AsyncMock(return_value=True)
    ports = SimpleNamespace(memory=SimpleNamespace(remember_explicit=writer))
    session = SimpleNamespace(
        active=SimpleNamespace(turn_session_id="chat-7"),
        viewed_session_id="chat-7",
    )
    finish = AsyncMock()
    with patch("chat_memory.finish_chat_turn", finish):
        decision = await MemoryChatRoute().before_intent(
            ports, object(), session, "/remember Cedar",
            is_resume=False, resume_state=None, reserved=False,
        )
    assert decision.handled is True
    writer.assert_awaited_once_with("Cedar", session_id="chat-7")


@pytest.mark.asyncio
async def test_explicit_memory_uses_canonical_revision_ledger(tmp_path):
    ports = _ports(tmp_path)
    assert await remember_explicit(
        ports, "  Prefer concise bullets  ", session_id="chat-1"
    )
    rows = ports.store.retrieve("chat-2", "concise bullets")
    assert rows[0]["content"] == "Prefer concise bullets"
    assert rows[0]["revision"] == 1


@pytest.mark.asyncio
async def test_recall_combines_approved_memory_and_conversation_search(tmp_path):
    ports = _ports(tmp_path)
    pending = ports.store.propose(
        "chat-a", kind="add", content="The deployment lane is Cedar"
    )
    sid = ports.sessions.create_session()
    ports.sessions.append_messages(sid, [
        {"role": "user", "text": "Cedar release notes"},
        {"role": "assistant", "text": "Recorded"},
    ])
    before = await retrieve_memory(
        ports, {"query": "Cedar", "_chat_id": "chat-a"}
    )
    assert "[approved]" not in before
    ports.store.approve(pending["proposal_id"], actor="user")
    after = await retrieve_memory(
        ports, {"query": "Cedar", "_chat_id": "chat-b", "limit": 99}
    )
    assert "[approved]" in after
    assert "deployment lane is Cedar" in after
    assert len([line for line in after.splitlines() if line.startswith("- [")]) <= 6


def test_dynamic_memory_and_previous_run_receipt_are_bounded_beside_request():
    text = append_dynamic_memory_context(
        "Current request", "m" * 5000, "p" * 5000
    )
    assert text.startswith("Current request\n\n---\nHarness context")
    assert "current user request takes precedence" in text
    assert len(text) < 4700


@pytest.mark.asyncio
async def test_extraction_never_receives_assistant_reply(tmp_path):
    calls = []

    class Router:
        engine_ready = True
        mode = "local"

        def cloud_route_ready(self):
            return False

        async def stream(self, messages, **_kwargs):
            calls.append(messages)
            yield '{"profile":["The user prefers tea"],"memories":[]}'

    ports = _ports(tmp_path, router=Router())
    proposals = await extract_and_store(
        ports,
        "I prefer tea.",
        "You should use a one-pass coding workflow.",
        evidence={"session_id": "chat-1"},
    )
    assert len(calls) == 1
    assert "I prefer tea" in calls[0][1]["content"]
    assert "one-pass coding workflow" not in calls[0][1]["content"]
    assert [item.content for item in proposals] == ["The user prefers tea"]


@pytest.mark.asyncio
async def test_ordinary_question_skips_memory_model_inference(tmp_path):
    class Router:
        engine_ready = True
        mode = "local"

        def cloud_route_ready(self):
            return False

        async def stream(self, _messages, **_kwargs):
            raise AssertionError("ordinary questions must not launch memory inference")
            yield

    await extract_and_store(
        _ports(tmp_path, router=Router()),
        "What is 17 x 24?", "408", evidence={"session_id": "chat-1"},
    )
