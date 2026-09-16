from __future__ import annotations

from types import SimpleNamespace

import pytest

import ws_dispatch
from chat_session import ConnectionSession
from goals import create_goal_service
from memory_store import MemoryStore
from tests.support.conversation_sessions import open_sessions
from work_fabric.service import WorkService


class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


@pytest.mark.asyncio
async def test_memory_ui_and_runs_project_goals(tmp_path):
    store = MemoryStore(str(tmp_path / "memory.sqlite3"))
    sessions = open_sessions(tmp_path / "sessions")
    chat_id = sessions.create_session()
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    goals = create_goal_service(work)
    runtime = SimpleNamespace(
        goals=goals,
        sessions=sessions,
        memory=SimpleNamespace(store=store, consolidate_once=lambda: 0),
    )
    host = SimpleNamespace(
        data_dir=str(tmp_path),
        require_runtime=lambda: runtime,
    )
    session = ConnectionSession(viewed_session_id=chat_id)
    socket = Socket()

    await ws_dispatch.HANDLERS["memory:core:set"](
        host, socket, session, {"text": "Prefer concise bullets"}
    )
    assert socket.messages[-1]["items"][0]["text"] == "Prefer concise bullets"

    await ws_dispatch.HANDLERS["memory:loops:create"](
        host, socket, session, {"title": "Ship", "goal": "Ship the report"}
    )
    goal_projection = next(
        item for item in socket.messages if item.get("type") == "memory:loop"
    )
    assert goal_projection["projection"] == "goal"
    assert goals.get(goal_projection["item"]["id"]).objective == "Ship the report"
    assert not hasattr(host, "loop_memory")
