from __future__ import annotations

from types import SimpleNamespace

import pytest

import ws_dispatch
from chat_session import ConnectionSession
from goals import create_goal_service
from tests.support.conversation_sessions import open_sessions
from work_fabric.service import WorkService


class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


@pytest.mark.asyncio
async def test_goal_ui_preserves_records_without_fact_memory(tmp_path):
    sessions = open_sessions(tmp_path / "sessions")
    chat_id = sessions.create_session()
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    goals = create_goal_service(work)
    runtime = SimpleNamespace(
        goals=goals,
        sessions=sessions,
    )
    host = SimpleNamespace(
        data_dir=str(tmp_path),
        require_runtime=lambda: runtime,
    )
    session = ConnectionSession(viewed_session_id=chat_id)
    socket = Socket()

    assert not any(name.startswith("memory:") for name in ws_dispatch.HANDLERS)

    await ws_dispatch.HANDLERS["goals:loops:create"](
        host, socket, session, {"title": "Ship", "goal": "Ship the report"}
    )
    goal_projection = next(
        item for item in socket.messages if item.get("type") == "goals:loop"
    )
    assert goal_projection["projection"] == "goal"
    assert goals.get(goal_projection["item"]["id"]).objective == "Ship the report"
    assert not hasattr(host, "loop_memory")
