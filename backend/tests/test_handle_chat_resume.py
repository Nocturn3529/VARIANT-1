"""Native snapshot resume-path smoke tests for _handle_chat."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server

from agent_engine.snapshot_utils import RUNSTATE_RESUME_CONTEXT_HEADER
from agent_engine.sqlite_snapshot_store import SQLiteRunSnapshotStore
from agent_engine.state import new_run_state
from agent_task import TaskStatus
from chat_session import ConnectionSession


async def _handle_chat(*args, **kwargs):
    return await server.APP.require_runtime().chat.handle_chat(*args, **kwargs)

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
    router.model_name = ""
    router.cfg = {"vision": {}}
    router.stream = stream_fn
    return router


@contextmanager
def _patch_chat_runtime(**changes):
    chat = server.APP.require_runtime().chat
    with ExitStack() as stack:
        for name, replacement in changes.items():
            stack.enter_context(patch.object(chat, name, replacement))
        yield


def _save_resume_snapshot(
    db_path,
    *,
    task_id="snapshot-task",
    goal="open Notepad and type hello",
    disclosed_tools=None,
    chat_id=None,
    updated_at=None,
):
    state = new_run_state(source="chat", title=goal, goal=goal, config_name="chat_task_default")
    now = time.time()
    state["run_id"] = task_id
    state["thread_id"] = task_id
    state["created_at"] = now - 60
    state["updated_at"] = float(updated_at if updated_at is not None else now - 5)
    state["status"] = TaskStatus.IN_PROGRESS.value
    # Resume fixtures carry the same pinned machine revision as new chats.
    state["graph_revision"] = "chat.ipython.v2"
    state["messages"] = [
        {"role": "system", "content": "old system prompt"},
        {"role": "user", "content": goal},
        {"role": "assistant", "content": "Opened Notepad."},
    ]
    state["task"] = {
        "task_id": task_id,
        "goal": goal,
        "status": TaskStatus.IN_PROGRESS.value,
        "checkpoint_created_at": now - 60,
        "model_name": "",
    }
    state["tools"] = {"disclosed_names": list(disclosed_tools or ["computer"])}
    state["chat_id"] = str(
        server.APP.require_runtime().sessions.get_active()
        if chat_id is None else chat_id
    )
    store = SQLiteRunSnapshotStore(str(db_path))
    store.commit_boundary_sync(
        state,
        completed_node="model_step",
        next_node="main_finalize",
        expected_head_sequence=None,
    )
    return state


@pytest.fixture
def session():
    return ConnectionSession()


@pytest.fixture
def fake_ws():
    return FakeWebSocket()


@pytest.mark.asyncio
async def test_resume_with_native_snapshot_restores_goal_and_messages(tmp_path, monkeypatch, fake_ws, session):
    db_path = tmp_path / "snapshots.sqlite3"
    _save_resume_snapshot(db_path)
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))
    captured: list[list] = []

    async def fake_stream(messages, **kwargs):
        # Keep the actual first-call prompt snapshot this test is asserting.
        captured.append([dict(message) for message in messages])
        yield "Resumed and finished."

    mock_registry = MagicMock()
    mock_registry.specs.return_value = FAKE_TOOL_SPEC
    with (
        _patch_chat_runtime(
            vision_state=lambda: (False, "text"),
            extract_and_store=AsyncMock(),
        ),
        patch.object(server.APP, "router", _mock_router(fake_stream)),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "vision_cfg", return_value=VISION_OFF),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
        patch.object(server.APP, "new_run", return_value={"id": "run-resume", "step": 0}),
    ):
        await _handle_chat(fake_ws, "resume", session)

    assert captured
    assert RUNSTATE_RESUME_CONTEXT_HEADER in captured[0][0]["content"]
    assert "open Notepad and type hello" in captured[0][0]["content"]
    assert captured[0][-1]["role"] == "user"
    assert captured[0][-1]["content"] == "TASK STATE: in_progress"
    assert [m for m in fake_ws.messages if m.get("type") == "done"]


@pytest.mark.asyncio
async def test_resume_blocked_while_busy(fake_ws, session):
    session.busy = True
    with patch.object(server.APP, "router", _mock_router(AsyncMock())):
        await _handle_chat(fake_ws, "resume", session)

    done = [m for m in fake_ws.messages if m.get("type") == "done"]
    assert done
    assert "already running" in done[-1]["text"].lower()
    assert not [m for m in fake_ws.messages if m.get("type") == "start"]


@pytest.mark.asyncio
async def test_resume_without_native_snapshot_returns_message(tmp_path, monkeypatch, fake_ws, session):
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(tmp_path / "empty.sqlite3"))

    with patch.object(server.APP, "router", _mock_router(AsyncMock())):
        await _handle_chat(fake_ws, "resume", session)

    done = [m for m in fake_ws.messages if m.get("type") == "done"]
    assert done
    assert "resume" in done[-1]["text"].lower()
    assert not [m for m in fake_ws.messages if m.get("type") == "start"]


@pytest.mark.asyncio
async def test_resume_flag_without_matching_text_uses_native_snapshot(tmp_path, monkeypatch, fake_ws, session):
    db_path = tmp_path / "snapshots.sqlite3"
    _save_resume_snapshot(db_path)
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))
    captured: list[list] = []

    async def fake_stream(messages, **kwargs):
        captured.append(messages)
        yield "Done."

    mock_registry = MagicMock()
    mock_registry.specs.return_value = FAKE_TOOL_SPEC
    with (
        _patch_chat_runtime(
            vision_state=lambda: (False, "text"),
            extract_and_store=AsyncMock(),
        ),
        patch.object(server.APP, "router", _mock_router(fake_stream)),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "vision_cfg", return_value=VISION_OFF),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
        patch.object(server.APP, "new_run", return_value=None),
    ):
        await _handle_chat(fake_ws, "go", session, resume=True)

    assert captured
    assert RUNSTATE_RESUME_CONTEXT_HEADER in captured[0][0]["content"]


@pytest.mark.asyncio
async def test_ordinary_follow_up_inherits_interrupted_internal_graph(
    tmp_path, monkeypatch, fake_ws, session,
):
    db_path = tmp_path / "snapshots.sqlite3"
    runtime = server.APP.require_runtime()
    sid = runtime.sessions.create_session()
    session.viewed_session_id = sid
    _save_resume_snapshot(
        db_path,
        task_id="stopped-review",
        goal="review the repository",
        chat_id=sid,
    )
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))
    monkeypatch.setattr(runtime.session_runtimes, "_snapshot_store", SQLiteRunSnapshotStore(str(db_path)))
    runtime.sessions.append_messages(sid, [
        {"role": "user", "text": "review the repository"},
        {"role": "assistant", "text": "Task stopped."},
    ])
    runtime.sessions.set_last_run_receipt(sid, {
        "run_id": "stopped-review", "status": "cancelled", "settled": True,
    })
    captured: list[list[dict]] = []

    async def fake_stream(messages, **_kwargs):
        captured.append([dict(message) for message in messages])
        yield "The prior inspection found a concrete issue."

    with (
        _patch_chat_runtime(
            vision_state=lambda: (False, "text"),
            extract_and_store=AsyncMock(),
        ),
        patch.object(server.APP, "router", _mock_router(fake_stream)),
        patch.object(server.APP, "mem_query", new=AsyncMock(return_value=[])),
        patch.object(server.APP, "vision_cfg", return_value=VISION_OFF),
        patch.object(server.APP, "emit_activity", new=AsyncMock()),
        patch.object(server.APP, "new_run", return_value=None),
    ):
        await _handle_chat(fake_ws, "what did you find?", session)

    assert captured
    first_request = captured[0]
    assert first_request[0]["role"] == "system"
    assert {"role": "user", "content": "review the repository"} in first_request
    assert {"role": "assistant", "content": "Opened Notepad."} in first_request
    assert first_request[-1]["role"] == "user"
    assert first_request[-1]["content"].split("\n\n---\n", 1)[0].endswith("what did you find?")
    assert first_request[-1]["content"].count("what did you find?") == 1
    assert first_request[-1]["content"].count("<variant1_current_context>") == 1
    assert runtime.sessions.get_context_projection(sid)["purpose"] == "stopped_evidence"


def test_orphaned_task_payload_from_native_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "snapshots.sqlite3"
    _save_resume_snapshot(
        db_path,
        task_id="snapshot-orphan",
    )
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))

    payload = server.APP.require_runtime().chat.orphaned_task_payload()

    assert payload["task_id"] == "snapshot-orphan"
    assert payload["goal"] == "open Notepad and type hello"
    assert "milestones_done" not in payload


def test_snapshot_resume_state_reads_durable_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "snapshots.sqlite3"
    _save_resume_snapshot(db_path, task_id="snapshot-resume", goal="continue native task")
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))

    state, err = server.APP.require_runtime().chat.snapshot_resume_state()

    assert err == ""
    assert state is not None
    assert state["task"]["task_id"] == "snapshot-resume"
    assert state["task"]["goal"] == "continue native task"
    assert state["tools"]["disclosed_names"] == ["computer"]
    # Loading an old snapshot without verified stopped-context coverage must
    # not fabricate the new host-context provenance revision.
    assert "host_context_extents_revision" not in state


def test_newer_non_chat_snapshots_do_not_hide_main_chat_resume(tmp_path, monkeypatch):
    db_path = tmp_path / "snapshots.sqlite3"
    _save_resume_snapshot(
        db_path,
        task_id="chat-behind-workers",
        goal="continue the interrupted chat task",
    )
    store = SQLiteRunSnapshotStore(str(db_path))
    now = time.time()
    for i in range(60):
        thread_id = f"automation:{i}"
        state = new_run_state(
            source="automation",
            title="background work",
            goal="background work",
            thread_id=thread_id,
            run_id=f"automation-{i}",
        )
        state["status"] = "running"
        state["updated_at"] = now + i
        store.commit_boundary_sync(
            state,
            completed_node="init",
            next_node="prepare",
            expected_head_sequence=None,
        )
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))

    state, err = server.APP.require_runtime().chat.snapshot_resume_state()

    assert err == ""
    assert state is not None
    assert state["task"]["task_id"] == "chat-behind-workers"


def test_resume_scan_is_scoped_to_active_chat(tmp_path, monkeypatch):
    db_path = tmp_path / "snapshots.sqlite3"
    sessions = server.APP.require_runtime().sessions
    active_chat = sessions.get_active()
    other_chat = sessions.create_session(
        "other snapshot owner", make_active=False
    )
    now = time.time()
    _save_resume_snapshot(
        db_path,
        task_id="active-chat-task",
        goal="resume the active chat only",
        chat_id=active_chat,
        updated_at=now - 20,
    )
    _save_resume_snapshot(
        db_path,
        task_id="other-chat-newer-task",
        goal="must remain attached to the other chat",
        chat_id=other_chat,
        updated_at=now - 1,
    )
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(db_path))

    state, err = server.APP.require_runtime().chat.snapshot_resume_state()

    assert err == ""
    assert state is not None
    assert state["chat_id"] == active_chat
    assert state["task"]["task_id"] == "active-chat-task"

    other_state, other_err = (
        server.APP.require_runtime().chat.snapshot_resume_state(other_chat)
    )
    assert other_err == ""
    assert other_state is not None
    assert other_state["chat_id"] == other_chat
    assert other_state["task"]["task_id"] == "other-chat-newer-task"


def test_orphaned_task_payload_without_native_snapshot_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("VARIANT1_AGENT_SNAPSHOT_DB", str(tmp_path / "empty.sqlite3"))

    payload = server.APP.require_runtime().chat.orphaned_task_payload()

    assert payload is None
