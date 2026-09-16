from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.support.conversation_sessions import open_sessions
from session_catalog.profiles import ACTION_SURFACE
from session_catalog.support import SupportMatrix, SupportRule
from llm_router import LLMRouter
from model_runtime.context import (
    context_limit_tokens,
    normalize_model_route,
    session_model_route,
)
from session_context import latest_session_context
from session_projection import current_projection, project_session_conversation
import ws_dispatch


def _append_exchange(store, sid, user_text, assistant_text):
    return store.append_messages(sid, [
        {"role": "user", "text": user_text},
        {"role": "assistant", "text": assistant_text},
    ])


def _router(tmp_path):
    cfg = {
        "mode": "local",
        "local": {"model": "", "ctx_size": 16384},
        "cloud": {
            "provider": "anthropic",
            "anthropic_model": "claude-sonnet-4-6",
            "openai_model": "gpt-4o",
            "provider_options": {},
        },
        "sampling": {"max_tokens": 512},
    }
    return LLMRouter(cfg, str(tmp_path), config_path=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", ["normal", "lost_ack", "post_apply_error"])
async def test_scoped_settings_apply_fences_admission_and_has_authoritative_recovery(tmp_path, monkeypatch, settlement):
    import asyncio
    from unittest.mock import AsyncMock
    from chat_session import ConnectionSession
    from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
    import session_projection
    import session_context
    lose_ack = settlement == "lost_ack"
    if settlement == "post_apply_error":
        def broken_context(*args, **kwargs):
            raise RuntimeError("context reply formatting failed after durable apply")
        monkeypatch.setattr(session_context, "estimated_session_context", broken_context)

    router = _router(tmp_path)
    router._support_matrix = SupportMatrix((SupportRule(profile=ACTION_SURFACE,
        provider="anthropic", model="claude-sonnet-4-6", adapter="*", status="developer"),))
    sessions = open_sessions(tmp_path / "chats")
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtimes.sqlite3")))
    sessions.bind_runtime_lifecycle(registry.ensure_runtime)
    sid = sessions.create_session()
    original = {"mode":"local", "provider":"local", "model":""}
    sessions.set_model_route(sid, original)
    runtime = SimpleNamespace(sessions=sessions, session_runtimes=registry,
        chat=SimpleNamespace(viewed_session_id=lambda session:session.viewed_session_id))
    srv = SimpleNamespace(router=router, require_runtime=lambda:runtime,
        _compress_messages=None, hub=SimpleNamespace(broadcast=AsyncMock()))
    session = ConnectionSession(viewed_session_id=sid)
    entered, release = asyncio.Event(), asyncio.Event()
    async def prepare(*args, **kwargs):
        entered.set()
        await release.wait()
        return []
    monkeypatch.setattr(session_projection, "project_session_conversation", prepare)
    class Socket(_Socket):
        async def send_json(self, message):
            if lose_ack and message.get("type") == "session:settings:ack":
                raise ConnectionError("acknowledgement lost")
            await super().send_json(message)
    socket = Socket()
    pending = asyncio.create_task(ws_dispatch.HANDLERS["mode:set"](srv, socket, session,
        {"scope":"session", "id":sid, "request_id":"model-change", "mode":"cloud",
         "provider":"anthropic", "model":"claude-sonnet-4-6"}))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert registry.configuration_pending(sid)
        snapshot = _Socket()
        await ws_dispatch.HANDLERS["session:settings:get"](srv, snapshot, session,
            {"session_id":sid, "request_id":"read-during"})
        assert snapshot.messages[-1]["pending"] is True
        assert snapshot.messages[-1]["route"]["mode"] == "local"
        with pytest.raises(RuntimeError, match="configuration is pending"):
            registry.set_mutation_write_enabled(sid, True, actor="test", expected_revision=0)
        assert not registry.runtime(sid).mutation_write_enabled
        overlapping = _Socket()
        await ws_dispatch.HANDLERS["mode:set"](srv, overlapping, session,
            {"scope":"session", "id":sid, "request_id":"overlap", "mode":"cloud"})
        assert overlapping.messages[-1]["status"] == "rejected"
        assert overlapping.messages[-1]["code"] == "session_configuration_pending"
        assert sessions.get_model_route(sid)["mode"] == "local"
        send = _Socket()
        await ws_dispatch.HANDLERS["chat"](srv, send, session,
            {"session_id":sid, "client_id":"send-during-change", "text":"Do not admit yet"})
        assert send.messages[-1]["error"] == "session_configuration_pending"
        assert send.messages[-1]["client_id"] == "send-during-change"
        assert not registry.is_busy(sid) and not registry.repository.list_tickets(sid)
        release.set()
        if lose_ack:
            with pytest.raises(ConnectionError, match="lost"):
                await pending
        else:
            await pending
            assert socket.messages[-1]["status"] == "applied"
            assert socket.messages[-1]["request_id"] == "model-change"
            assert socket.messages[-1]["session_id"] == sid
        await ws_dispatch.HANDLERS["session:settings:get"](srv, snapshot, session,
            {"session_id":sid, "request_id":"recover"})
        actual = snapshot.messages[-1]
        assert actual["pending"] is False and actual["route"]["mode"] == "cloud"
        applied = sessions.get_model_route(sid)
        rejected = _Socket()
        await ws_dispatch.HANDLERS["reasoning:effort:set"](srv, rejected, session,
            {"id":sid, "request_id":"bad-effort", "effort":"not-a-real-effort"})
        assert rejected.messages[-1]["status"] == "rejected"
        assert rejected.messages[-1]["request_id"] == "bad-effort"
        assert sessions.get_model_route(sid) == applied
        assert not registry.configuration_pending(sid)
    finally:
        release.set()
        if not pending.done(): pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


def test_bound_model_route_is_task_local_and_restores_defaults(tmp_path):
    router = _router(tmp_path)
    assert router.mode == "local"
    assert context_limit_tokens(router) == 16384

    route = {"mode": "cloud", "provider": "openai", "model": "gpt-4o"}
    with router.bind_model_route(route):
        assert router.mode == "cloud"
        assert router.cloud_provider == "openai"
        assert router.get_cloud_model() == "gpt-4o"
        assert router.active_model_name() == "gpt-4o"
        assert router.context_limit_tokens() == 128000

    assert router.mode == "local"
    assert router.cloud_provider == "anthropic"


def test_reasoning_effort_is_declared_and_persisted_on_the_session_route(tmp_path):
    router = _router(tmp_path)
    xai = normalize_model_route(router, {
        "mode": "cloud", "provider": "xai", "model": "grok-4.6",
        "reasoning_effort": "high",
    })
    assert xai["reasoning_efforts"] == ["low", "medium", "high"]
    assert xai["reasoning_effort"] == "high"

    store = open_sessions(tmp_path / "reasoning-chats")
    sid = store.create_session()
    assert store.set_model_route(sid, xai)
    selected = session_model_route(store, sid, router)
    assert selected["reasoning_effort"] == "high"
    with router.bind_model_route(selected):
        assert router.get_reasoning_effort("xai", "grok-4.6") == "high"

    spark = normalize_model_route(router, {
        "mode": "cloud", "provider": "openai-codex",
        "model": "gpt-5.3-codex-spark",
    })
    luna = normalize_model_route(router, {
        "mode": "cloud", "provider": "openai-codex",
        "model": "gpt-5.6-luna",
    })
    assert "max" not in spark["reasoning_efforts"]
    assert spark["reasoning_effort"] == "xhigh"
    assert "max" in luna["reasoning_efforts"]
    assert luna["reasoning_effort"] == "max"


def test_context_override_wins_and_unknown_cloud_model_stays_unknown(tmp_path):
    router = _router(tmp_path)
    router.cfg["cloud"]["context_windows"] = {
        "custom/my-private-model": 77777,
    }
    custom = {"mode": "cloud", "provider": "custom", "model": "my-private-model"}
    assert router.context_limit_tokens(custom) == 77777
    unknown = {"mode": "cloud", "provider": "custom", "model": "unknown"}
    assert router.context_limit_tokens(unknown) == 0
    assert router.projection_budget_tokens(unknown) == 32768


def test_session_state_keeps_route_and_projection_separate_from_transcript(tmp_path):
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    _append_exchange(store, sid, "hello", "hi")
    route = {"mode": "cloud", "provider": "openai", "model": "gpt-4o"}
    assert store.set_model_route(sid, route)
    assert store.get_model_route(sid) == route
    assert store.set_context_projection(
        sid,
        [{"role": "user", "content": "summary"}],
        source_message_count=2,
        context_limit_tokens=128000,
    )
    assert store.get_context_projection(sid)["messages"][0]["content"] == "summary"
    assert [row["text"] for row in store.get_session(sid)["messages"]] == ["hello", "hi"]


@pytest.mark.asyncio
async def test_projection_compacts_then_appends_new_canonical_turns(tmp_path):
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    for index in range(14):
        _append_exchange(
            store,
            sid,
            f"question {index} " + "x" * 520,
            f"answer {index} " + "y" * 520,
        )

    async def compress(messages, **_kwargs):
        return [
            {"role": "user", "content": "[Earlier steps in this task, summarized]\nkept facts"},
            *messages[-6:],
        ]

    projected = await project_session_conversation(
        store,
        sid,
        mode="local",
        context_limit_tokens=8192,
        compress_messages=compress,
    )
    assert len(projected) == 7
    assert len(store.recent_convo(sid, None)) == 28

    _append_exchange(store, sid, "new question", "new answer")
    rehydrated, source_count = current_projection(store, sid)
    assert source_count == 30
    assert rehydrated[-2:] == [
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]


def test_context_meter_recalculates_when_session_route_changes(tmp_path):
    router = _router(tmp_path)
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    _append_exchange(store, sid, "hello", "hi")
    target = {"mode": "local", "provider": "local", "model": "local.gguf"}
    store.set_model_route(sid, target)
    projected, _ = current_projection(store, sid)
    old_manifest = {
        "run": {"session_id": sid},
        "route": {
            "selected_mode": "cloud", "provider": "openai",
            "model": "gpt-4o",
        },
    }
    snapshot = latest_session_context(
        {"items": [old_manifest]},
        sid,
        context_limit_tokens=16384,
        route=session_model_route(store, sid, router),
        projected_messages=projected,
    )
    assert snapshot["route"] == "local"
    assert snapshot["context_limit_tokens"] == 16384
    assert snapshot["measurement"] == "estimated_projection"


class _Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_session_model_switch_is_rejected_while_turn_is_active(tmp_path):
    socket = _Socket()
    session = SimpleNamespace(busy=True, viewed_session_id="s1")
    await ws_dispatch.HANDLERS["mode:set"](
        SimpleNamespace(require_runtime=lambda: SimpleNamespace(
            session_runtimes=SimpleNamespace(is_busy=lambda _sid: True))), socket, session,
        {"type": "mode:set", "scope": "session", "id": "s1", "mode": "cloud"},
    )
    assert socket.messages[-1]["code"] == "session_busy_model_switch"


@pytest.mark.asyncio
async def test_session_model_switch_persists_without_changing_global_default(tmp_path):
    router = _router(tmp_path)
    router._support_matrix = SupportMatrix((
        SupportRule(
            profile=ACTION_SURFACE,
            provider="anthropic",
            model="claude-sonnet-4-6",
            adapter="*",
            status="developer",
        ),
    ))
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    socket = _Socket()
    session = SimpleNamespace(busy=True, viewed_session_id=sid,
                              active=SimpleNamespace(runtime_chat_id="other-running-chat"))
    record = SimpleNamespace(
        identity=SimpleNamespace(action_surface=ACTION_SURFACE),
        mutation_write_enabled=False,
        mutation_authority_revision=0,
    )
    runtime = SimpleNamespace(
        sessions=store,
        session_runtimes=SimpleNamespace(
            is_busy=lambda chat_id: chat_id == "other-running-chat",
            ensure_runtime=lambda _sid: record,
        ),
    )
    srv = SimpleNamespace(
        router=router,
        require_runtime=lambda: runtime,
        viewed_chat_sid=lambda _session: sid,
        _compress_messages=None,
    )
    await ws_dispatch.HANDLERS["mode:set"](
        srv, socket, session,
        {"type": "mode:set", "scope": "session", "id": sid, "mode": "cloud"},
    )
    assert router.mode == "local"
    assert store.get_model_route(sid)["mode"] == "cloud"
    assert socket.messages[-1]["type"] == "chat:context"
    assert socket.messages[-1]["route"] == "cloud"


@pytest.mark.asyncio
async def test_session_model_switch_uses_effective_mutation_qualification(tmp_path):
    router = _router(tmp_path)
    router._support_matrix = SupportMatrix((
        SupportRule(
            profile=ACTION_SURFACE,
            provider="xai",
            model="grok-4.6",
            adapter="*",
            status="developer",
        ),
    ))
    store = open_sessions(tmp_path / "chats")
    sid = store.create_session()
    socket = _Socket()
    session = SimpleNamespace(busy=False, viewed_session_id=sid)
    record = SimpleNamespace(
        identity=SimpleNamespace(action_surface=ACTION_SURFACE),
        mutation_write_enabled=True,
        mutation_authority_revision=2,
    )
    srv = SimpleNamespace(
        router=router,
        require_runtime=lambda: SimpleNamespace(
            sessions=store,
            session_runtimes=SimpleNamespace(
                is_busy=lambda _sid: False,
                ensure_runtime=lambda _sid: record,
            ),
        ),
        _compress_messages=None,
    )

    await ws_dispatch.HANDLERS["mode:set"](
        srv,
        socket,
        session,
        {
            "type": "mode:set",
            "scope": "session",
            "id": sid,
            "mode": "cloud",
            "provider": "xai",
            "model": "grok-4.6",
            "reasoning_effort": "low",
        },
    )

    assert not any(
        message.get("code") == "unsupported_model_route"
        for message in socket.messages
    )
    assert store.get_model_route(sid)["mode"] == "cloud"
    assert store.get_model_route(sid)["model"] == "grok-4.6"
    assert store.get_model_route(sid)["reasoning_effort"] == "low"
