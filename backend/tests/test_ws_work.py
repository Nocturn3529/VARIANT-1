"""Work Fabric WebSocket replay and correlated mutation contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import ws_dispatch
from work_fabric.scope import WorkScope
from work_fabric.service import WorkService


class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


def _host(tmp_path):
    work = WorkService.open(str(tmp_path / "work.sqlite3"), worker_id="ws-test")
    runtime = SimpleNamespace(work=work)
    return SimpleNamespace(
        require_runtime=lambda: runtime,
        sessions=SimpleNamespace(get_active=lambda: "chat-1"),
    ), work


def _session():
    return SimpleNamespace(
        viewed_session_id="chat-1",
        active=SimpleNamespace(runtime_chat_id="", turn_session_id=""),
    )


@pytest.mark.asyncio
async def test_work_snapshot_replays_cursor_and_scoped_jobs(tmp_path):
    host, work = _host(tmp_path)
    job = work.jobs.create(
        "test.wait",
        owner_kind="chat",
        owner_id="chat-1",
        scope=WorkScope(chat_id="chat-1"),
    )
    socket = Socket()

    await ws_dispatch.HANDLERS["work:get"](
        host,
        socket,
        _session(),
        {"type": "work:get", "request_id": "request-1", "after_sequence": 0},
    )

    message = socket.messages[-1]
    assert message["type"] == "work:snapshot"
    assert message["request_id"] == "request-1"
    assert message["chat_id"] == "chat-1"
    assert message["cursor"] >= 1
    assert [item["job_id"] for item in message["jobs"]] == [job.job_id]
    assert message["events"][0]["type"] == "job.created"
    assert "database" not in message["runtime"]


@pytest.mark.asyncio
async def test_work_event_cursor_advances_across_unrelated_full_page(tmp_path):
    host, work = _host(tmp_path)
    for index in range(2):
        work.events.publish(
            "other.event",
            aggregate_kind="test",
            aggregate_id=f"other-{index}",
            scope=WorkScope(chat_id="other-chat"),
        )
    wanted = work.events.publish(
        "wanted.event",
        aggregate_kind="test",
        aggregate_id="wanted",
        scope=WorkScope(chat_id="chat-1"),
    )
    socket = Socket()

    await ws_dispatch.HANDLERS["work:events"](
        host, socket, _session(),
        {"type": "work:events", "request_id": "page-1", "limit": 1},
    )
    first = socket.messages[-1]
    assert first["events"] == []
    assert first["cursor"] > 0

    await ws_dispatch.HANDLERS["work:events"](
        host, socket, _session(), {
            "type": "work:events", "request_id": "page-2", "limit": 1,
            "after_sequence": first["cursor"],
        },
    )
    second = socket.messages[-1]
    assert second["events"][0]["sequence"] == wanted.sequence
    assert second["cursor"] == wanted.sequence


@pytest.mark.asyncio
async def test_work_cancel_requires_request_id_and_expected_revision(tmp_path):
    host, work = _host(tmp_path)
    job = work.jobs.create(
        "test.wait",
        owner_kind="chat",
        owner_id="chat-1",
        scope=WorkScope(chat_id="chat-1"),
    )
    socket = Socket()

    await ws_dispatch.HANDLERS["work:job:cancel"](
        host,
        socket,
        _session(),
        {
            "type": "work:job:cancel",
            "request_id": "request-2",
            "job_id": job.job_id,
            "expected_version": job.revision,
            "reason": "user stopped it",
        },
    )

    message = socket.messages[-1]
    assert message["type"] == "work:accepted"
    assert message["request_id"] == "request-2"
    assert message["job"]["status"] == "cancelled"
    assert message["job"]["revision"] > job.revision
