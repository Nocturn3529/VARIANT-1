"""Regressions for verified Grok findings; no live desktop/model calls."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import sys

import httpx
import pytest

from chat_finalize import persist_unfinalized_turn
from chat_session import ConnectionSession, detached_message_error, turn_chat_id
from desktop import input_primitives as inputs, vision_bridge
from desktop_fabric.models import DesktopScopeMismatch, DesktopStaleReference, DesktopUnavailable
from tests.support.conversation_sessions import open_sessions
from tests.test_desktop_fabric import runtime
from work_fabric.scope import WorkScope


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_field", ["turn_session_id", "runtime_chat_id", "viewed_session_id", "missing"])
async def test_early_terminal_never_writes_to_globally_selected_chat(tmp_path, owner_field):
    sessions = open_sessions(tmp_path / "chats")
    owner = sessions.create_session()
    selected = sessions.create_session()
    session = ConnectionSession()
    session.active.turn_display_user_text = "partial work"
    if owner_field == "viewed_session_id":
        session.viewed_session_id = owner
    elif owner_field != "missing":
        setattr(session.active, owner_field, owner)
    assert turn_chat_id(session) == (owner if owner_field != "missing" else "")
    host = SimpleNamespace(sessions=sessions, hub=SimpleNamespace(broadcast=AsyncMock()))
    durable = await persist_unfinalized_turn(host, SimpleNamespace(send_json=AsyncMock()), session, "Stopped")
    assert durable is (owner_field != "missing")
    assert sessions.get_session(selected)["messages"] == []
    assert len(sessions.get_session(owner)["messages"]) == (2 if durable else 0)


def test_late_annotation_targets_its_run_and_unknown_run_never_falls_back(tmp_path):
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    for run in ("first", "second"):
        sessions.append_messages(sid, [{"role": "user", "text": run},
            {"role": "assistant", "text": run + " reply", "run_id": run}])
    receipt = {"durationMs": 8000}
    sessions.annotate_last_assistant(sid, run_id="first", receipt=receipt)
    rows = sessions.get_session(sid)["messages"]
    assert rows[1]["receipt"]["durationMs"] == 8000
    assert "receipt" not in rows[3]
    assert sessions.annotate_last_assistant(sid, run_id="missing", receipt=receipt) is None
    assert sessions.get_session(sid)["messages"] == rows


@pytest.mark.parametrize("kind", ["chat:session:delete", "chat:session:rename", "chat:session:pin",
    "chat:session:archive", "chat:session:annotate", "reasoning:effort:set", "chat:runtime:mutation:set"])
def test_detached_id_alias_is_pinned(kind):
    assert detached_message_error({"type": kind, "id": "other"}, "owner") == "detached_chat_owner_mismatch"
    assert detached_message_error({"type": kind, "id": "owner"}, "owner") == ""
    assert detached_message_error({"type": "terminal:write", "session_id": "owner", "chat_id": "other"}, "owner")


def test_literal_typing_stops_before_next_character_on_focus_loss(monkeypatch):
    delivered = []
    auto = SimpleNamespace(KeyboardInput=lambda *args: args)
    monkeypatch.setattr(inputs, "_send_input_batch", lambda events: (delivered.extend(events) or len(events)))
    monkeypatch.setattr(inputs.time, "sleep", lambda _: None)
    def guard():
        if delivered:
            raise DesktopStaleReference("focus moved")
    with pytest.raises(DesktopStaleReference):
        inputs._type_text_direct(auto, "abcd", guard=guard)
    assert len(delivered) == 2
    assert delivered[0][1] == ord("a")


def test_key_sequence_uses_installed_grammar_and_releases_modifier_on_focus_loss(monkeypatch):
    auto = pytest.importorskip("uiautomation")
    parser = sys.modules[auto.SendKeys.__module__]
    delivered = []
    monkeypatch.setattr(parser, "keybd_event", lambda key, scan, flags, extra: delivered.append((key, flags)))
    monkeypatch.setattr(parser, "_VKtoSC", lambda key: key)
    monkeypatch.setattr(parser.time, "sleep", lambda _: None)
    def guard():
        if delivered:
            raise DesktopStaleReference("focus moved")
    with pytest.raises(DesktopStaleReference):
        inputs._send_keys_direct(auto, "CTRL+S", guard=guard)
    assert len(delivered) == 2
    assert delivered[0][0] == delivered[1][0] == parser.SpecialKeyNames["CTRL"]
    assert not delivered[0][1] & 2 and delivered[1][1] & 2


def test_failed_bound_capture_never_calls_monitor_capture(monkeypatch):
    ctx = SimpleNamespace(session=SimpleNamespace(target_window=object(), target_meta={"title": "Editor"}),
        _locked_target_alive=lambda _: (False, "window_closed"))
    locked = Mock(side_effect=AssertionError("must not capture monitor"))
    active = Mock(side_effect=AssertionError("must not capture monitor"))
    monkeypatch.setattr(vision_bridge.vc, "grab_monitor_for_hwnd_png", locked)
    monkeypatch.setattr(vision_bridge.vc, "grab_active_monitor_png", active)
    bundle = vision_bridge.vision_capture_on_uia_thread(ctx)
    assert bundle.png == b"" and bundle.meta.mode == "unavailable"
    locked.assert_not_called()
    active.assert_not_called()


@pytest.mark.asyncio
async def test_observation_is_chat_owned_and_survives_reopen(tmp_path):
    fabric, fake = runtime(tmp_path)
    a, b = WorkScope(chat_id="a"), WorkScope(chat_id="b")
    observation = await fabric.observe(fake.window.window_id, scope=a)
    from desktop_fabric.repository import DesktopFabricRepository
    reopened = DesktopFabricRepository(fabric.repository.path)
    assert reopened.get_observation(observation.observation_id, scope=a).scope == a
    with pytest.raises(DesktopScopeMismatch):
        reopened.get_observation(observation.observation_id, scope=b)
    with pytest.raises(DesktopScopeMismatch):
        await fabric.act(fake.window.window_id, action="set_value", arguments={"value": "bad"},
            scope=b, source_observation=observation)
    assert fake.dispatches == 0


@pytest.mark.asyncio
async def test_missing_image_preserves_uia_evidence(tmp_path):
    fabric, fake = runtime(tmp_path)
    fake.capture = AsyncMock(side_effect=DesktopUnavailable("window minimized"))
    view = await fabric.observe(fake.window.window_id, scope=WorkScope(chat_id="a"))
    assert view.elements and not view.image_ref
    assert view.capture["error"] == "window minimized"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///C:/secret.txt", "javascript:alert(1)", "data:text/html,hello", "https://user:secret@example.com"])
async def test_managed_navigation_rejects_unsupported_scheme_before_dispatch(url):
    from browser_fabric.adapters import ManagedPlaywrightAdapter
    from browser_fabric.models import BrowserValidationError
    adapter = object.__new__(ManagedPlaywrightAdapter)
    page = SimpleNamespace(goto=AsyncMock())
    with pytest.raises(BrowserValidationError):
        await adapter._perform_core(page, "navigate", {"url": url})
    page.goto.assert_not_awaited()


def test_health_check_keeps_localhost_and_does_not_follow_redirect(monkeypatch):
    from execution_hosts.service import ProcessService
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://other.invalid/"})
    real_client = httpx.Client
    monkeypatch.setattr("execution_hosts.service.httpx.Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    record = SimpleNamespace(recipe=SimpleNamespace(health_check={"kind": "http", "url": "http://127.0.0.1:8000/", "statuses": [200]}))
    ok, detail = ProcessService._check_health(record)
    assert not ok and detail["http_status"] == 302
    assert calls == ["http://127.0.0.1:8000/"]
    record.recipe.health_check["url"] = "file:///C:/secret.txt"
    assert ProcessService._check_health(record)[0] is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_invalid_local_model_switch_leaves_current_runtime_running(tmp_path):
    from model_runtime.llama_server import LlamaServer, LocalEngineError
    engine = LlamaServer({}, str(tmp_path), str(tmp_path))
    engine.stop = AsyncMock()
    engine.start = AsyncMock()
    with pytest.raises(LocalEngineError):
        await engine.restart(str(tmp_path / "missing.gguf"))
    engine.stop.assert_not_awaited()
    model = tmp_path / "models" / "user" / "chosen.gguf"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"GGUF")
    await engine.restart("chosen.gguf")
    assert engine.model == str(model)
    engine.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_goal_cannot_execute_in_another_chat():
    from goals.host_handlers import GoalHostHandlers
    handlers = object.__new__(GoalHostHandlers)
    result = await handlers.python(SimpleNamespace(step=SimpleNamespace(config={"code": "x=1", "runtime_chat_id": "b"}),
        goal=SimpleNamespace(owner_chat_id="a")))
    assert result.status == "blocked" and "owning chat" in result.error


@pytest.mark.parametrize("method,path,upstream", [
    ("GET", "/v1/models", "models"), ("GET", "/v1/status", "status"), ("GET", "/status", "status"),
    ("POST", "/v1/chat/completions", "chat_completions"),
    ("POST", "/v1/tokenize", "tokenize"), ("POST", "/tokenize", "tokenize"),
])
def test_gateway_routes_require_bearer_before_touching_inference(monkeypatch, method, path, upstream):
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    import server
    call = AsyncMock(return_value=JSONResponse({"ok": True}))
    monkeypatch.setattr(server.openai_gateway, upstream, call)
    client = TestClient(server.app, client=("127.0.0.1", 50000))
    assert client.request(method, path).status_code == 401
    assert client.request(method, path, headers={"authorization": "Bearer wrong"}).status_code == 401
    call.assert_not_awaited()
    assert client.request(method, path, headers={"authorization": "Bearer " + server.AUTH_TOKEN}).status_code == 200
    call.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_rejects_chunked_oversize_and_invalid_json_before_inference(monkeypatch):
    from fastapi import HTTPException
    from model_runtime import openai_gateway
    monkeypatch.setattr(openai_gateway, "MAX_GATEWAY_REQUEST_BYTES", 8)
    async def chunks():
        yield b"12345"
        yield b"6789"
        raise AssertionError("oversized body should stop being consumed")
    with pytest.raises(HTTPException) as caught:
        await openai_gateway._request_json(SimpleNamespace(stream=chunks))
    assert caught.value.status_code == 413
    async def bad():
        yield b"nope"
    with pytest.raises(HTTPException) as caught:
        await openai_gateway._request_json(SimpleNamespace(stream=bad))
    assert caught.value.status_code == 400


@pytest.mark.asyncio
async def test_model_manager_does_not_restart_for_invalid_candidate():
    from model_runtime import engine_manager
    from tests.test_engine_manager_lifecycle import _Engine, _Router, _Hub, _status
    engine = _Engine()
    engine.validate_selection = Mock(side_effect=ValueError("invalid GGUF selection"))
    router = _Router(engine)
    with pytest.raises(ValueError, match="invalid GGUF"):
        await engine_manager.restart_engine(router, _Hub(), lambda: _status(router), "bad.gguf")
    assert engine.restart_calls == [] and engine.stop_calls == 0 and engine.ready
    assert router.selected == []


def test_detached_lifecycle_id_is_rejected_through_real_socket():
    from fastapi.testclient import TestClient
    import server
    sessions = server.APP.require_runtime().sessions
    owner = sessions.create_session()
    other = sessions.create_session()
    client = TestClient(server.app)
    with client.websocket_connect(f"/ws?token={server.AUTH_TOKEN}&view_role=detached_chat&view_chat_id={owner}") as ws:
        ws.send_json({"type": "chat:session:delete", "id": other})
        for _ in range(10):
            event = ws.receive_json()
            if event.get("type") == "error":
                assert event["error"] == "detached_chat_owner_mismatch"
                break
        else:
            raise AssertionError("missing pinned-chat rejection")
    assert sessions.has_session(owner) and sessions.has_session(other)


def test_managed_git_disables_hooks_without_running_git(monkeypatch, tmp_path):
    import os
    from coding import git_process
    calls = []
    def fake_child(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"", output_limit_exceeded=False, timed_out=False)
    monkeypatch.setattr(git_process, "run_bounded_child", fake_child)
    git_process.GitProcess(executable="fixture-git").run(str(tmp_path), ["merge", "--ff-only", "fixture"], mutating=True)
    assert "core.hooksPath=" + os.devnull in calls[0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL identity")
def test_kernel_acl_grantee_uses_process_identity_not_environment(monkeypatch, tmp_path):
    from kernel_runtime import lease
    calls = []
    monkeypatch.setenv("USERNAME", "Everyone")
    monkeypatch.setattr("psutil.Process", lambda: SimpleNamespace(username=lambda: "DOMAIN\\actual-user"))
    monkeypatch.setattr(lease.os.path, "isfile", lambda _: True)
    monkeypatch.setattr(lease.subprocess, "run", lambda command, **kw: (calls.append(command) or SimpleNamespace(returncode=0)))
    lease._restrict_path(str(tmp_path))
    assert calls[0][-1] == "DOMAIN\\actual-user:F"


@pytest.mark.asyncio
async def test_named_focus_returns_a_durable_window_observation(tmp_path):
    from desktop_fabric.adapter import AdapterFocus, AdapterObservation
    fabric, fake = runtime(tmp_path)
    fake.focus_session = AsyncMock(return_value=AdapterFocus(
        result={"locator": True}, hwnd=fake.window.hwnd,
        observation=AdapterObservation(uia=(), uia_generation=99),
    ))
    original = fake.focus
    fake.focus = AsyncMock(wraps=original)
    result, window, view = await fabric.focus_session({"name": "Editor"}, scope=WorkScope(chat_id="a"))
    fake.focus.assert_awaited_once()
    assert window.window_id == fake.window.window_id
    assert view.elements and view.uia_generation != 99 and view.scope.chat_id == "a"
