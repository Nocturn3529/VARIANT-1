from __future__ import annotations

import pytest

from desktop_fabric.binding import (
    DesktopBinding,
    current_desktop_binding,
)
from agent_engine.state import new_run_state
from run_context import Variant1RunContext, bind_run_context, current_run_context


def test_new_run_state_accepts_inherited_run_id():
    st = new_run_state(
        source="chat", title="t", goal="g",
        run_id="abc123deadbe", thread_id="abc123deadbe",
    )
    assert st["run_id"] == "abc123deadbe"
    assert st["thread_id"] == "abc123deadbe"


def test_new_run_state_mints_when_no_run_id():
    st = new_run_state(source="chat", title="t", goal="g")
    assert st["run_id"].startswith("run_")
    assert st["thread_id"] == st["run_id"]


def test_explicit_chat_identity_keeps_its_runtime_profile_after_navigation():
    import server
    from chat_session import ConnectionSession

    runtime = server.APP.require_runtime()
    admitted = runtime.sessions.create_session()
    viewed = runtime.sessions.create_session()
    session = ConnectionSession(viewed_session_id=viewed)
    context = server.APP.make_run_context(
        "chat", "already admitted", session=session, inherit_parent=False,
        metadata={"chat_id": admitted},
    )

    assert context.session_id == admitted
    assert context.work_scope.chat_id == admitted
    assert context.metadata["runtime_identity"] == (
        runtime.session_runtimes.ensure_runtime(admitted).to_dict()
    )


def test_bind_run_context_sets_and_resets_desktop_binding():
    desktop = DesktopBinding(binding_id="desktop_binding_ctx_test")
    ctx = Variant1RunContext.create(
        source="chat",
        title="hello",
        desktop_binding=desktop,
    )

    assert current_run_context() is None
    assert current_desktop_binding() is None

    with bind_run_context(ctx):
        assert current_run_context() is ctx
        assert current_desktop_binding() is desktop

    assert current_run_context() is None
    assert current_desktop_binding() is None


@pytest.mark.asyncio
async def test_server_emit_activity_prefers_run_context(monkeypatch):
    import server

    sent = []

    async def broadcast(msg):
        sent.append(msg)

    monkeypatch.setattr(server.APP.hub, "broadcast", broadcast)
    ctx = Variant1RunContext.create(source="automation", title="auto")

    with bind_run_context(ctx):
        await server.APP.emit_activity("note", text="hello")

    assert sent[0]["run_id"] == ctx.run_id
    assert sent[0]["source"] == "automation"
    assert sent[0]["event"] == "note"


@pytest.mark.asyncio
async def test_server_emit_activity_allows_explicit_run_override(monkeypatch):
    import server

    sent = []

    async def broadcast(msg):
        sent.append(msg)

    monkeypatch.setattr(server.APP.hub, "broadcast", broadcast)
    ctx = Variant1RunContext.create(source="chat", title="outer")

    with bind_run_context(ctx):
        await server.APP.emit_activity(
            "task:done",
            run_id="task-run-1",
            source="task",
            status="ok",
        )

    assert sent[0]["run_id"] == "task-run-1"
    assert sent[0]["source"] == "task"
    assert sent[0]["status"] == "ok"


def test_server_background_context_does_not_inherit_user_run_or_desktop():
    import server

    user_desktop = DesktopBinding(binding_id="desktop_binding_user_ctx")
    user_ctx = Variant1RunContext.create(
        source="chat",
        title="user task",
        desktop_binding=user_desktop,
    )

    with bind_run_context(user_ctx):
        bg_ctx = server.APP.make_run_context(
            "automation",
            "scheduled",
            metadata={"_server_bound_kind": "automation"},
            inherit_parent=False,
            isolate_desktop=True,
        )

    assert bg_ctx.parent_run_id == ""
    assert bg_ctx.desktop_binding is not user_desktop
    assert bg_ctx.desktop_binding.binding_id != user_desktop.binding_id
    assert bg_ctx.desktop_binding.owner_kind == "run"

    with bind_run_context(bg_ctx):
        assert server.APP.require_bound_run_context(
            "automation", operation="test") is bg_ctx
        assert server.APP.require_bound_run_context(
            "passive", operation="test") is None
