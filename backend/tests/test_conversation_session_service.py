"""Canonical SQL chat-session behavior."""

from __future__ import annotations

import pytest

from chat_sessions import ChatSessionService, build_chat_sessions
from project_context import ProjectBindingError


def test_new_chat_request_replays_across_reconnect_and_restart_without_reset(tmp_path):
    import pytest
    from chat_sessions.models import ConversationTombstoned
    path = str(tmp_path / "conversations.sqlite3")
    first = build_chat_sessions(path=path)
    created = first.create_session(request_id="deck-new-1")
    first.rename(created, "Renamed after creation")
    first.set_model_route(created, {"mode":"cloud", "provider":"test", "model":"chosen"})
    second = build_chat_sessions(path=path)
    replay = second.create_session(request_id="deck-new-1")
    assert replay == created
    assert len(second.list_sessions()) == 1
    assert second.get_session(replay)["title"] == "Renamed after creation"
    assert second.get_model_route(replay)["model"] == "chosen"
    assert second.create_session() != second.create_session()
    second.delete(replay)
    with pytest.raises(ConversationTombstoned):
        first.create_session(request_id="deck-new-1")


def test_chat_service_owns_session_and_transcript_projection(tmp_path):
    path = tmp_path / "conversations.sqlite3"
    service = build_chat_sessions(path=str(path))
    assert isinstance(service, ChatSessionService)
    session_id = service.create_session("Canonical")
    service.append_messages(session_id, [
        {"role": "user", "text": "hello", "ticket_id": "ticket-1"},
        {"role": "assistant", "text": "hi", "mood": "warm"},
    ])

    restarted = build_chat_sessions(path=str(path))
    session = restarted.get_session(session_id)

    assert session is not None
    assert [item["text"] for item in session["messages"]] == ["hello", "hi"]
    assert restarted.has_message_ticket(session_id, "ticket-1")
    assert not (tmp_path / "chat_sessions").exists()


def test_session_state_is_sql_owned_and_runtime_creation_is_explicit(tmp_path):
    service = build_chat_sessions(path=str(tmp_path / "conversations.sqlite3"))
    created: list[tuple[str, bool]] = []
    service.bind_runtime_lifecycle(
        lambda session_id, *, is_new: created.append((session_id, is_new))
    )
    session_id = service.create_session("State")
    assert service.set_model_route(
        session_id,
        {"mode": "cloud", "provider": "openai", "model": "gpt-test"},
    )

    reopened = build_chat_sessions(path=str(tmp_path / "conversations.sqlite3"))

    assert created == [(session_id, True)]
    assert reopened.get_model_route(session_id)["model"] == "gpt-test"


def test_chat_project_binding_is_durable_and_new_chats_are_unbound(tmp_path):
    path = str(tmp_path / "conversations.sqlite3")
    project_root = tmp_path / "project"
    project_root.mkdir()
    service = build_chat_sessions(path=path)
    session_id = service.create_session("Bound")

    assert service.get_session(session_id)["project"] is None
    assert service.list_sessions()[0]["project"] is None
    expected = {"root": str(project_root.resolve()), "name": "project"}
    assert service.set_project(session_id, str(project_root)) == expected

    reopened = build_chat_sessions(path=path)
    assert reopened.get_project(session_id) == expected
    assert reopened.get_session(session_id)["project"] == expected
    assert reopened.list_sessions()[0]["project"] == expected
    independent = reopened.create_session("Independent")
    assert reopened.get_session(independent)["project"] is None

    assert reopened.set_project(session_id, None) is None
    assert build_chat_sessions(path=path).get_project(session_id) is None


def test_chat_project_binding_rejects_noncanonical_roots(tmp_path):
    service = build_chat_sessions(path=str(tmp_path / "conversations.sqlite3"))
    session_id = service.create_session()
    regular_file = tmp_path / "not-a-directory.txt"
    regular_file.write_text("fixture", encoding="utf-8")

    with pytest.raises(ProjectBindingError, match="absolute"):
        service.set_project(session_id, "relative/project")
    with pytest.raises(ProjectBindingError, match="existing directory"):
        service.set_project(session_id, str(regular_file))
    with pytest.raises(ProjectBindingError, match="string or null"):
        service.set_project(session_id, 42)


def test_append_if_absent_is_one_sql_ticket_boundary(tmp_path):
    service = build_chat_sessions(path=str(tmp_path / "conversations.sqlite3"))
    session_id = service.create_session()

    _, first = service.append_if_absent(
        session_id,
        "ticket-1",
        {"role": "user", "text": "only once"},
    )
    _, replay = service.append_if_absent(
        session_id,
        "ticket-1",
        {"role": "user", "text": "duplicate"},
    )

    assert first is True
    assert replay is False
    assert [
        item["text"] for item in service.get_session(session_id)["messages"]
    ] == ["only once"]


def test_transcript_id_makes_terminal_turn_replay_idempotent(tmp_path):
    service = build_chat_sessions(path=str(tmp_path / "conversations.sqlite3"))
    session_id = service.create_session()

    service.append_messages(session_id, [
        {"role": "user", "text": "do it", "transcript_id": "run-stable"},
        {"role": "assistant", "text": "finished"},
    ])
    service.append_messages(session_id, [
        {"role": "user", "text": "replayed", "transcript_id": "run-stable"},
        {"role": "assistant", "text": "duplicate"},
    ])

    messages = service.get_session(session_id)["messages"]
    assert [(row["role"], row["text"]) for row in messages] == [
        ("user", "do it"),
        ("assistant", "finished"),
    ]


def test_activity_annotation_preserves_call_identity_and_timing(tmp_path):
    service = build_chat_sessions(path=str(tmp_path / "conversations.sqlite3"))
    session_id = service.create_session()
    service.append_messages(session_id, [
        {"role": "user", "text": "inspect it"},
        {"role": "assistant", "text": "done"},
    ])

    service.annotate_last_assistant(session_id, steps=[{
        "id": "step-1",
        "kind": "tool",
        "label": "Read file",
        "status": "error",
        "tool": "read_file",
        "callId": "call-exact",
        "rawStatus": "invalid_arguments",
        "argsPreview": '{"path":"demo.txt"}',
        "resultPreview": "path is required",
        "startedAt": 1_700_000_000_000,
        "completedAt": 1_700_000_000_125,
        "durationMs": 125,
        "admissionMs": 4,
        "ts": 1_700_000_000_125,
    }])

    step = service.get_session(session_id)["messages"][-1]["steps"][0]
    assert step["call_id"] == "call-exact"
    assert step["raw_status"] == "invalid_arguments"
    assert step["args_preview"] == '{"path":"demo.txt"}'
    assert step["result_preview"] == "path is required"
    assert step["started_at"] == 1_700_000_000_000
    assert step["completed_at"] == 1_700_000_000_125
    assert step["duration_ms"] == 125
    assert step["admission_ms"] == 4
