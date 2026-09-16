"""Typed chat stages fail closed and keep observability content-free."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import chat_pipeline
from chat_session import ConnectionSession
from tests.support.conversation_sessions import open_sessions
from chat_attachments import MAX_ATTACH_TEXT_TOTAL_CHARS
from chat_context_stage import project_chat_prompt
from chat_context_lineage import ChatContextEvidence, build_chat_context_receipt
from chat_setup_stage import prepare_chat_turn_stage
from chat_stage_result import ChatStageContinue, ChatStageDone
from prompt_builder import PromptContext, PromptProjection


def test_attachment_lineage_uses_the_shared_limit_without_prompt_content():
    secret = "private-attachment-body"
    suffix = "x" * MAX_ATTACH_TEXT_TOTAL_CHARS
    receipt = build_chat_context_receipt(ChatContextEvidence(
        is_resume=False,
        conversation=[],
        base_system="system",
        prompt_context=SimpleNamespace(),
        composer_text="inspect this",
        model_text="inspect this" + suffix,
        attachment_suffix=suffix,
        display_attachments=[{"name": "notes.txt"}],
        attachment_text=secret + suffix,
        memories=[],
        catalog_specs=[],
        disclosed_tool_specs=[],
    ))

    attachment = next(
        item for item in receipt["items"] if item["kind"] == "attachment")
    assert attachment["truncated"] is True
    assert attachment["reason"] == "size_limit"
    assert secret not in str(receipt)


@pytest.mark.parametrize("count", [0, 8, 12, 40])
def test_history_lineage_reports_the_actual_prepared_projection(count):
    receipt = build_chat_context_receipt(ChatContextEvidence(
        is_resume=False,
        conversation=[{"role": "user", "content": "private history"}] * count,
        base_system="system", prompt_context=SimpleNamespace(),
        composer_text="follow up", model_text="follow up", attachment_suffix="",
        display_attachments=[], attachment_text="", memories=[],
        catalog_specs=[], disclosed_tool_specs=[],
    ))
    history = next(item for item in receipt["selections"] if item["kind"] == "conversation_history")
    assert history["considered"] == history["kept"] == count
    assert history["dropped"] == 0
    assert history["reason"] == "session_projection"
    assert "private history" not in str(receipt)


def test_chat_prompt_keeps_instructions_stable_and_fresh_context_beside_raw_user_text():
    raw_user_text = "Open the report in the requested viewer."
    first_system, first_model_text = project_chat_prompt(
        PromptContext(
            profile_block="Prefers short summaries.",
            project_instructions="Run focused checks.",
            datetime="Monday 10:00",
            cwd=r"C:\workspace-one",
            project_roots=(r"C:\workspace-one",),
        ),
        PromptProjection(
            stable="STABLE ASTB CONTRACT",
            current="Current capability mount: Build",
        ),
        raw_user_text,
        memory_block="Remember the report is local.",
    )
    second_system, second_model_text = project_chat_prompt(
        PromptContext(
            profile_block="Now prefers detailed summaries.",
            project_instructions="Run the complete focused checks.",
            datetime="Tuesday 11:00",
            cwd=r"D:\workspace-two",
            project_roots=(r"D:\workspace-two",),
        ),
        PromptProjection(
            stable="STABLE ASTB CONTRACT",
            current="Current capability mount: Explore",
        ),
        raw_user_text,
        memory_block="Remember the report moved.",
    )

    assert first_system == second_system
    assert "Monday 10:00" not in first_system
    assert r"C:\workspace-one" not in first_system
    assert first_model_text.startswith(raw_user_text + "\n\n---\n")
    assert second_model_text.startswith(raw_user_text + "\n\n---\n")
    assert "<variant1_current_context>" in first_model_text
    assert "Monday 10:00" in first_model_text
    assert r"D:\workspace-two" in second_model_text
    assert "Current capability mount: Build" in first_model_text
    assert "Current capability mount: Explore" in second_model_text
    assert "Run the complete focused checks." in second_model_text
    assert first_model_text != second_model_text
    assert raw_user_text == "Open the report in the requested viewer."


@pytest.mark.asyncio
async def test_setup_stage_returns_typed_done_for_an_empty_turn():
    class _Socket:
        async def send_json(self, _payload):
            raise AssertionError("orchestrator, not setup, sends terminal payloads")

    session = SimpleNamespace(active=SimpleNamespace())
    outcome = await prepare_chat_turn_stage(
        SimpleNamespace(),
        _Socket(),
        "",
        session,
        resume=False,
        reserved=False,
        client_id="deck-1",
        source="",
        images=None,
        attachment_text="",
    )

    assert isinstance(outcome, ChatStageDone)
    assert outcome.payload["type"] == "done"
    assert outcome.payload["source"] == "chat"


def _setup_terminal_ports(tmp_path, *, engine_ready=True, vision_ready=True):
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    router = SimpleNamespace(
        mode="local",
        engine_ready=engine_ready,
        cloud_route_ready=lambda: False,
    )
    return sid, store, SimpleNamespace(
        io=SimpleNamespace(
            router=router,
            sessions=store,
            hub=SimpleNamespace(broadcast=AsyncMock()),
        ),
        vision=SimpleNamespace(
            vision_state=lambda: (vision_ready, "local"),
        ),
        session=SimpleNamespace(
            snapshot_resume_state=lambda _chat_id: (None, "no checkpoint"),
            compress_messages=AsyncMock(return_value=[]),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "text", "expected_reply"),
    [
        ("engine", "Explain this", "isn't loaded"),
        ("resume", "resume", "No interrupted task"),
        ("empty", "", "Send a message"),
    ],
)
async def test_setup_done_paths_persist_the_visible_terminal_exchange(
    tmp_path, kind, text, expected_reply,
):
    sid, store, ports = _setup_terminal_ports(
        tmp_path,
        engine_ready=kind != "engine",
        vision_ready=True,
    )
    session = ConnectionSession(viewed_session_id=sid)
    websocket = SimpleNamespace(send_json=AsyncMock())

    await chat_pipeline._handle_chat_body(
        ports,
        websocket,
        text,
        session,
        resume=kind == "resume",
        reserved=True,
        client_id="deck-setup",
        source="chat",
        images=None,
        attachment_text="",
    )

    terminal = websocket.send_json.await_args.args[0]
    assert terminal["type"] == "done"
    assert expected_reply in terminal["text"]
    messages = store.get_session(sid)["messages"]
    if kind == "empty":
        assert [row["role"] for row in messages] == ["assistant"]
    else:
        assert [row["role"] for row in messages] == ["user", "assistant"]
        assert messages[0]["text"]
    assert messages[-1]["text"] == terminal["text"]


@pytest.mark.asyncio
async def test_setup_admits_images_when_selected_model_is_not_prequalified_for_vision(
    tmp_path,
):
    sid, _store, ports = _setup_terminal_ports(
        tmp_path,
        engine_ready=True,
        vision_ready=False,
    )
    session = ConnectionSession(viewed_session_id=sid)
    websocket = SimpleNamespace(send_json=AsyncMock())

    outcome = await prepare_chat_turn_stage(
        ports,
        websocket,
        "Inspect this image",
        session,
        resume=False,
        reserved=True,
        client_id="deck-image",
        source="chat",
        images=[{"data_b64": "aW1hZ2U=", "media_type": "image/png"}],
        attachment_text="",
    )

    assert isinstance(outcome, ChatStageContinue)
    assert websocket.send_json.await_args.args[0]["type"] == "start"
