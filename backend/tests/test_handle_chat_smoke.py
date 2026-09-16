"""Thin smoke test for the _handle_chat task path (mocked deps, no real LLM)."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import chat_pipeline
from observability import context_lineage
import prompt_builder
import server
from chat_session import ConnectionSession


async def _handle_chat(*args, **kwargs):
    return await server.APP.require_runtime().chat.handle_chat(*args, **kwargs)


async def _chat_task(*args, **kwargs):
    return await server.APP.require_runtime().chat.run_task(*args, **kwargs)

FAKE_TOOL_SPEC = [
    {
        "name": "computer",
        "description": "Operate the current desktop",
        "params": {"name": {"type": "string", "required": True}},
        "group": "desktop",
    }
]

VISION_OFF = {
    "route": "local",
    "local_capable": False,
    "local_route": "single",
    "cloud_route": "single",
}

VISION_SINGLE = {
    **VISION_OFF,
    "local_capable": True,
    "local_route": "single",
}

_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNg"
    "YAAAAAMAASsJTYQAAAAASUVORK5CYII="
)


class FakeWebSocket:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_json(self, data):
        self.messages.append(data)


def _mock_router(stream_fn):
    router = MagicMock()
    router.mode = "local"
    router.engine_ready = True
    router.has_cloud_key = MagicMock(return_value=False)
    router.reasoning = False
    router.cfg = {"vision": {}}
    router.stream = stream_fn
    # Real string, not an auto-speccing MagicMock attribute: the graph path's
    # durable checkpoint write serializes this (via current_model()), and a
    # MagicMock isn't msgpack-serializable.
    router.model_name = "test-model"
    return router


def _mock_registry():
    mock_registry = MagicMock()
    mock_registry.specs.return_value = FAKE_TOOL_SPEC
    return mock_registry


@contextmanager
def _patch_runtime(*, chat=None, models=None):
    runtime = server.APP.require_runtime()
    with ExitStack() as stack:
        for service, changes in (
            (runtime.chat, chat or {}),
            (runtime.models, models or {}),
        ):
            for name, replacement in changes.items():
                stack.enter_context(patch.object(service, name, replacement))
        yield


@pytest.fixture
def session():
    return ConnectionSession()


@pytest.fixture
def fake_ws():
    return FakeWebSocket()


@pytest.mark.asyncio
async def test_handle_chat_task_path_completes_without_crash(fake_ws, session):
    """Task-classified input runs the loop stub and finishes with a done message."""
    captured: list[list] = []

    async def fake_stream(messages, **kwargs):
        captured.append(messages)
        yield "Opened Notepad."

    mock_registry = MagicMock()
    mock_registry.specs.return_value = FAKE_TOOL_SPEC
    with (
        _patch_runtime(chat={
            "vision_state": lambda: (False, "text"),
            "extract_and_store": AsyncMock(),
        }),
        patch.object(server.APP, "router", _mock_router(fake_stream)),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "vision_cfg", return_value=VISION_OFF),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
        patch.object(server.APP, "new_run", return_value={"id": "run-test", "step": 0}),
    ):
        await _handle_chat(fake_ws, "open Notepad", session)

    assert session.busy is False
    assert session.active.task is None
    done = [m for m in fake_ws.messages if m.get("type") == "done"]
    assert done, "expected a done websocket message"
    assert done[-1]["text"]
    assert captured, f"ROUTER.stream should have been called; terminal frames: {fake_ws.messages[-3:]}"
    system_content = captured[0][0]["content"]
    assert "CURRENT TASK" not in system_content
    assert "Use the available native tools" not in system_content
    model_user = next(
        msg for msg in captured[0]
        if msg.get("role") == "user"
    )
    assert model_user["content"].startswith(
        "open Notepad\n\n---\nHarness context"
    )
    assert "<variant1_current_context>" in model_user["content"]
    sid = session.viewed_session_id or server.APP.require_runtime().sessions.get_active()
    persisted = server.APP.require_runtime().sessions.get_session(sid)["messages"]
    assert persisted[-2]["role"] == "user"
    assert persisted[-2]["text"] == "open Notepad"
    receipt = context_lineage.receipt_from_messages(captured[0])
    assert receipt is not None
    assert receipt["purpose"] == "main_chat_step"
    selection_kinds = {row["kind"] for row in receipt["selections"]}
    assert {
        "conversation_history", "retrieved_memory", "tool_schema",
    } <= selection_kinds
    assert any(
        row["kind"] == "current_user" for row in receipt["items"]
    )


@pytest.mark.asyncio
async def test_handle_chat_greeting_stays_casual_and_does_not_capture_vision(fake_ws, session):
    calls = []

    async def fake_stream(messages, **kwargs):
        calls.append(kwargs)
        yield "Hey."

    emit = AsyncMock()
    capture = AsyncMock(return_value="screen-b64")

    with (
        _patch_runtime(
            chat={
                "vision_state": lambda: (True, "single"),
                "extract_and_store": AsyncMock(),
            },
        ),
        patch.object(server.APP.require_runtime().desktop, "capture", new=capture),
        patch.object(server.APP, "router", _mock_router(fake_stream)),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "vision_cfg", return_value=VISION_SINGLE),
        patch.object(server.APP, "emit_activity", new=emit),
    ):
        await _handle_chat(fake_ws, "hey there", session)

    capture.assert_not_called()
    assert len(calls) == 1
    # The IPython surface keeps one stable provider schema for every turn, including a
    # greeting. Native task-specific schemas still stay out of the request.
    assert [tool.get("name") for tool in (calls[0].get("tools") or ())] == ["ipython"]
    done = [m for m in fake_ws.messages if m.get("type") == "done"]
    assert done[-1]["text"] == "Hey."


@pytest.mark.asyncio
async def test_desktop_task_does_not_capture_foreground_screen_automatically(fake_ws, session):
    async def fake_stream(messages, **kwargs):
        yield "Ready."

    capture = AsyncMock(return_value="unrelated-screen-b64")
    with (
        _patch_runtime(
            chat={
                "vision_state": lambda: (True, "single"),
                "extract_and_store": AsyncMock(),
            },
        ),
        patch.object(server.APP.require_runtime().desktop, "capture", new=capture),
        patch.object(server.APP, "router", _mock_router(fake_stream)),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "vision_cfg", return_value=VISION_SINGLE),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
        patch.object(server.APP, "new_run", return_value={"id": "run-test", "step": 0}),
    ):
        await _handle_chat(fake_ws, "open Notepad", session)

    capture.assert_not_called()


@pytest.mark.asyncio
async def test_handle_chat_screen_words_never_capture_automatically(fake_ws, session):
    captured_kwargs = []

    async def fake_stream(messages, **kwargs):
        captured_kwargs.append(kwargs)
        yield "I can see your screen."

    emit = AsyncMock()
    capture = AsyncMock(return_value=_TINY_PNG_B64)

    with (
        _patch_runtime(
            chat={
                "vision_state": lambda: (True, "single"),
                "extract_and_store": AsyncMock(),
            },
        ),
        patch.object(server.APP.require_runtime().desktop, "capture", new=capture),
        patch.object(server.APP, "router", _mock_router(fake_stream)),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "vision_cfg", return_value=VISION_SINGLE),
        patch.object(server.APP, "emit_activity", new=emit),
    ):
        await _handle_chat(fake_ws, "what do you see on my screen now?", session)

    capture.assert_not_called()
    assert not captured_kwargs[-1].get("image_b64")
    done = [m for m in fake_ws.messages if m.get("type") == "done"]
    assert done[-1]["text"] == "I can see your screen."


@pytest.mark.asyncio
async def test_user_image_attachment_is_labeled_and_sent_to_model(fake_ws, session):
    captured_messages = []
    captured_kwargs = []

    async def fake_stream(messages, **kwargs):
        captured_messages.append(messages)
        captured_kwargs.append(kwargs)
        yield "It is a tiny image."

    image = {
        "data_b64": _TINY_PNG_B64,
        "media_type": "image/png",
        "origin": "current_user",
    }
    with (
        _patch_runtime(chat={
            "vision_state": lambda: (True, "single"),
            "extract_and_store": AsyncMock(),
        }),
        patch.object(server.APP, "router", _mock_router(fake_stream)),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "vision_cfg", return_value=VISION_SINGLE),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
    ):
        await _handle_chat(
            fake_ws, "what is attached?", session, images=[image],
        )

    system_content = captured_messages[0][0]["content"]
    assert "## Attached images" not in system_content
    assert "## Screen" not in system_content
    model_user = next(
        msg for msg in captured_messages[0]
        if msg.get("role") == "user"
    )
    assert model_user["content"].startswith("what is attached?")
    assert "## Attached images" in model_user["content"]
    assert "The user attached one or more images" in model_user["content"]
    assert captured_kwargs[0]["image_b64"] == [image]
    receipt = context_lineage.receipt_from_messages(captured_messages[0])
    assert any(
        item["kind"] == "image_context" and item["reason"] == "user_attachment"
        for item in receipt["items"]
    )
    assert any(
        item["kind"] == "image_observation" and item["image_count"] == 1
        for item in receipt["items"]
    )


@pytest.mark.asyncio
async def test_unified_first_prompt_does_not_render_task_contract(fake_ws, session):
    lifecycle: list[str] = []
    from agent_task import Task as RealTask

    class TaskSpy(RealTask):
        def __init__(self, goal):
            lifecycle.append("task_init")
            super().__init__(goal)

        def render_static(self):
            lifecycle.append("render_static")
            return super().render_static()

    async def fake_stream(messages, **kwargs):
        yield "Done."

    mock_registry = MagicMock()
    mock_registry.specs.return_value = FAKE_TOOL_SPEC
    with (
        _patch_runtime(chat={
            "vision_state": lambda: (False, "text"),
            "extract_and_store": AsyncMock(),
        }),
        patch.object(server.APP, "Task", TaskSpy),
        patch.object(server.APP, "router", _mock_router(fake_stream)),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "vision_cfg", return_value=VISION_OFF),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
        patch.object(server.APP, "new_run", return_value=None),
    ):
        await _handle_chat(fake_ws, "open Notepad", session)

    assert lifecycle == ["task_init"]


@pytest.mark.asyncio
async def test_chat_task_cleans_busy_after_unexpected_error(fake_ws, session):
    seq = session.reserve_turn()

    async def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    with _patch_runtime(chat={"handle_chat": boom}):
        await _chat_task(fake_ws, "hello", session, turn_seq=seq)

    assert session.busy is False
    assert session.active.task is None
    done = [m for m in fake_ws.messages if m.get("type") == "done"]
    assert done
    assert "internal error" in done[-1]["text"].lower()
    assert "synthetic failure" not in done[-1]["text"]
    settled = [m for m in fake_ws.messages if m.get("type") == "run:settled"]
    assert len(settled) == 1
    assert fake_ws.messages.index(done[-1]) < fake_ws.messages.index(settled[0])
    assert settled[0]["terminal_reason"] == "harness_error"
    assert settled[0]["cause_class"] == "harness"
    assert settled[0]["receipt"]["settled"] is True
    assert server.APP.require_runtime().session_runtimes.is_busy(
        settled[0]["session_id"]
    ) is False


@pytest.mark.asyncio
async def test_finish_chat_turn_persists_and_broadcasts(fake_ws, session):
    """A finalized turn is written to CHAT_STORE and broadcast to other windows
    (tagged with the originating client_id so the sender can de-dupe)."""
    sid = server.APP.require_runtime().sessions.get_active()
    session.active.turn_session_id = sid
    session.active.turn_client_id = "deck-1"
    other = FakeWebSocket()
    server.APP.hub.add(other)
    try:
        with (
            _patch_runtime(chat={"extract_and_store": AsyncMock()}),
            patch.object(server.APP, "tts_enabled", return_value=False),
        ):
            await chat_pipeline.finish_chat_turn(
                server.APP.chat_ports(), fake_ws, session,
                "remember milk", "neutral", "Noted.")
    finally:
        server.APP.hub.remove(other)

    msgs = server.APP.require_runtime().sessions.get_session(
        sid
    )["messages"]
    assert msgs[-2]["text"] == "remember milk"
    assert msgs[-1]["text"] == "Noted."

    appended = [m for m in other.messages if m.get("type") == "chat:appended"]
    assert appended, "expected a chat:appended broadcast"
    assert appended[-1]["client_id"] == "deck-1"
    assert appended[-1]["user"]["text"] == "remember milk"
    assert appended[-1]["assistant"]["text"] == "Noted."


@pytest.mark.asyncio
async def test_finish_chat_turn_memory_failure_is_logged_after_durability(
    fake_ws,
    session,
    caplog,
):
    sid = server.APP.require_runtime().sessions.get_active()
    session.active.turn_session_id = sid
    failing_memory = MagicMock(side_effect=RuntimeError("memory backend offline"))

    with (
        _patch_runtime(chat={"extract_and_store": failing_memory}),
        patch.object(server.APP, "tts_enabled", return_value=False),
        caplog.at_level("ERROR", logger="chat_finalize"),
    ):
        await chat_pipeline.finish_chat_turn(
            server.APP.chat_ports(),
            fake_ws,
            session,
            "remember the durable result",
            "neutral",
            "Saved in chat.",
        )
        await session.active.post_turn_task

    messages = server.APP.require_runtime().sessions.get_session(
        sid
    )["messages"]
    assert messages[-1]["text"] == "Saved in chat."
    assert session.active.turn_persisted is True
    assert "post-turn memory extraction failed" in caplog.text


@pytest.mark.asyncio
async def test_chat_task_skips_stale_queued_turn(fake_ws, session):
    stale_seq = session.reserve_turn()
    latest_seq = session.reserve_turn()
    called = False

    async def should_not_run(*args, **kwargs):
        nonlocal called
        called = True

    with patch.object(
        server.APP.require_runtime().chat,
        "handle_chat",
        new=should_not_run,
    ):
        await _chat_task(fake_ws, "older text", session, turn_seq=stale_seq)

    assert called is False
    assert session.busy is True
    assert session.latest_turn_seq == latest_seq
