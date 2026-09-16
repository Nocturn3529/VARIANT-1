from __future__ import annotations

from types import SimpleNamespace

import pytest

from ws_protocol import CorrelatedResponder, request_id, session_work_scope


class _Socket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def test_request_id_is_trimmed_and_bounded():
    assert request_id({"request_id": "  request-1  "}) == "request-1"
    assert request_id({"request_id": "x" * 600}) == "x" * 512
    assert request_id({}) == ""


def test_session_work_scope_uses_the_viewed_chat_identity():
    session = SimpleNamespace(
        active=SimpleNamespace(runtime_chat_id="chat-active", turn_session_id="turn"),
        viewed_session_id="chat-viewed",
    )

    scope = session_work_scope(session)

    assert scope.chat_id == "chat-viewed"
    assert scope.to_dict(include_empty=False) == {"chat_id": "chat-viewed"}


@pytest.mark.asyncio
async def test_all_ui_domain_scope_helpers_follow_viewed_chat():
    import ws_execution, ws_work, ws_memory, ws_kernel
    session = SimpleNamespace(active=SimpleNamespace(runtime_chat_id="A", turn_session_id="A"),
                              viewed_session_id="B")
    execution_srv = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(
            sessions=SimpleNamespace(has_session=lambda chat_id: chat_id == "B"),
        ),
    )
    assert ws_execution._chat_id(
        execution_srv, session, {"chat_id": "B"},
    ) == "B"
    for module in (ws_work, ws_memory, ws_kernel):
        assert module._chat_id(None, session) == "B"


def test_busy_model_route_checks_the_requested_chat():
    from ws_protocol import chat_is_busy
    runtime = SimpleNamespace(session_runtimes=SimpleNamespace(is_busy=lambda chat_id: chat_id == "A"))
    session = SimpleNamespace(busy=True, active=SimpleNamespace(runtime_chat_id="A"), viewed_session_id="B")
    assert chat_is_busy(runtime, session, "A")
    assert not chat_is_busy(runtime, session, "B")


@pytest.mark.asyncio
async def test_correlated_responder_preserves_success_envelope():
    socket = _Socket()
    responder = CorrelatedResponder("artifact", "variant1.artifact-command.v1")

    async def action():
        return {"artifact_id": "artifact-1"}

    await responder(socket, {"request_id": "request-1"}, "create", action)

    assert socket.messages == [{
        "type": "artifact:accepted",
        "schema": "variant1.artifact-command.v1",
        "request_id": "request-1",
        "operation": "create",
        "result": {"artifact_id": "artifact-1"},
    }]


@pytest.mark.asyncio
async def test_correlated_responder_requires_ids_only_for_mutations():
    socket = _Socket()
    responder = CorrelatedResponder("artifact", "variant1.artifact-command.v1")
    calls = 0

    async def action():
        nonlocal calls
        calls += 1
        return []

    await responder(socket, {}, "create", action)
    await responder(socket, {}, "list", action, mutation=False)

    assert calls == 1
    assert socket.messages[0] == {
        "type": "artifact:rejected",
        "schema": "variant1.artifact-command.v1",
        "request_id": "",
        "operation": "create",
        "reason_code": "ValueError",
        "error": "request_id is required",
    }
    assert socket.messages[1]["type"] == "artifact:accepted"
    assert socket.messages[1]["result"] == []


@pytest.mark.asyncio
async def test_correlated_responder_preserves_domain_error_codes():
    class Conflict(RuntimeError):
        code = "revision_conflict"

    socket = _Socket()
    responder = CorrelatedResponder(
        "review", "variant1.review-command.v1", mutation_default=False
    )

    async def action():
        raise Conflict("review changed")

    await responder(socket, {}, "snapshot", action)

    assert socket.messages == [{
        "type": "review:rejected",
        "schema": "variant1.review-command.v1",
        "request_id": "",
        "operation": "snapshot",
        "reason_code": "revision_conflict",
        "error": "review changed",
    }]


@pytest.mark.asyncio
async def test_correlated_responder_captures_chat_owner_before_await():
    class Rejected(RuntimeError):
        code = "rejected"

    socket = _Socket()
    responder = CorrelatedResponder("terminal", "variant1.execution-command.v1")
    accepted_request = {"request_id": "accepted", "chat_id": "chat-a"}
    rejected_request = {"request_id": "rejected", "chat_id": "chat-a"}

    async def accepted():
        accepted_request["chat_id"] = "chat-b"
        return {"id": "terminal-1"}

    async def rejected():
        rejected_request["chat_id"] = "chat-b"
        raise Rejected("no")

    await responder(socket, accepted_request, "open", accepted)
    await responder(socket, rejected_request, "write", rejected)

    assert [row["chat_id"] for row in socket.messages] == ["chat-a", "chat-a"]
    assert socket.messages[0]["type"] == "terminal:accepted"
    assert socket.messages[1]["type"] == "terminal:rejected"
