"""Unit tests for pure ChatTurnPlan builders (issue #12)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chat_session import ActiveTurn, ConnectionSession
from chat_turn_plan import (
    ChatTurnPlan,
    apply_chat_turn_plan,
    build_attachment_plan,
    build_resume_plan,
    check_engine_ready,
    resolve_session_id,
    with_tool_catalog,
)


def _display(composer_text, *, attach_suffix="", has_image=False):
    text = (composer_text or "").strip()
    labels = [{"name": "a.txt", "kind": "text"}] if attach_suffix else []
    if not text and has_image:
        return "📷 Attached image", labels
    return text or "empty", labels


def test_attachment_plan_empty_without_image():
    out = build_attachment_plan(
        text="  ",
        attachment_text="",
        images=None,
        display_user_message=_display,
    )
    assert isinstance(out, tuple)
    assert "Send a message" in out[1]


def test_attachment_plan_image_only():
    plan = build_attachment_plan(
        text="",
        attachment_text="",
        images=[{"data_b64": "abc", "media_type": "image/png"}],
        display_user_message=_display,
    )
    assert not isinstance(plan, tuple)
    assert "attached image" in plan.model_text.lower()
    assert plan.user_images == ({"data_b64": "abc", "media_type": "image/png"},)


def test_attachment_plan_text_and_suffix():
    plan = build_attachment_plan(
        text="hello",
        attachment_text="\n\n[file body]",
        images=None,
        display_user_message=_display,
    )
    assert plan.model_text.startswith("hello")
    assert "[file body]" in plan.model_text
    assert plan.composer_text == "hello"


def test_engine_gate_local_not_ready():
    router = SimpleNamespace(
        mode="local",
        engine_ready=False,
        cloud_route_ready=lambda: False,
    )
    gate = check_engine_ready(router)
    assert not gate.ok
    assert "local model" in gate.text.lower()


def test_engine_gate_cloud_ok_with_key():
    router = SimpleNamespace(
        mode="cloud",
        engine_ready=False,
        cloud_route_ready=lambda: True,
    )
    assert check_engine_ready(router).ok


def test_resume_blocked_when_busy():
    plan = build_resume_plan(
        text="resume the task",
        resume_flag=False,
        session_busy=True,
        reserved=False,
        snapshot_resume_state=lambda: (None, "no checkpoint"),
        is_resume_request=lambda t: True,
    )
    assert plan.blocked
    assert "already running" in plan.blocked_text.lower()


def test_resume_hit():
    state = {"run_id": "r1", "task": {}}
    plan = build_resume_plan(
        text="resume",
        resume_flag=True,
        session_busy=False,
        reserved=False,
        snapshot_resume_state=lambda: (state, None),
        snapshot_follow_up_state=lambda: (state, None),
        is_resume_request=lambda t: False,
    )
    assert plan.is_resume
    assert plan.resume_state is state
    assert plan.resume_source == "native_snapshot"


def test_ordinary_follow_up_carries_interrupted_context_without_resuming_task():
    state = {"run_id": "r1", "task": {"status": "in_progress"}}
    plan = build_resume_plan(
        text="what did you find?",
        resume_flag=False,
        session_busy=False,
        reserved=False,
        snapshot_resume_state=lambda: (state, None),
        snapshot_follow_up_state=lambda: (state, None),
        is_resume_request=lambda _text: False,
    )
    assert not plan.requested
    assert not plan.is_resume
    assert plan.carry_context
    assert plan.resume_state is None
    assert plan.evidence_candidate is state
    assert plan.resume_source == "stopped_evidence"


def test_resume_miss_message():
    plan = build_resume_plan(
        text="resume",
        resume_flag=True,
        session_busy=False,
        reserved=False,
        snapshot_resume_state=lambda: (None, "no checkpoint"),
        is_resume_request=lambda t: False,
    )
    assert plan.blocked
    assert "No interrupted task" in plan.blocked_text


def test_resolve_session_id_fallback():
    store = MagicMock()
    store.has_session.return_value = False
    store.get_active.return_value = "active-id"
    assert resolve_session_id(store, "stale") == "active-id"


def test_apply_chat_turn_plan_atomic():
    session = ConnectionSession()
    attach = build_attachment_plan(
        text="hi",
        display_user_message=_display,
    )
    plan = ChatTurnPlan(
        client_id="deck-1",
        source="chat",
        session_id="s1",
        attachments=attach,
    )
    apply_chat_turn_plan(session, plan)
    active = session.active
    assert active.turn_client_id == "deck-1"
    assert active.turn_session_id == "s1"
    assert active.turn_display_user_text == "hi"
    session.clear_active_turn()
    assert session.active.turn_session_id is None


def test_with_tool_catalog_replaces_immutable_plan():
    plan = ChatTurnPlan(client_id="c")
    snap = SimpleNamespace(specs=[{"name": "computer"}], names={"computer"}, public_dict=lambda: {})
    plan2 = with_tool_catalog(
        plan,
        snapshot=snap,
    )
    assert plan2.tool_catalog is snap
    assert plan.tool_catalog is None
