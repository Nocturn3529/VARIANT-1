from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import tools
import ws_dispatch
from agent_engine.execution_context import run_context_from_state
from agent_engine.presets import chat_task_default
from agent_engine.state import new_run_state
from clarification import (
    interaction_request,
    normalize_questions,
    pending_goal_input,
    resolve_response,
    tool_ask_user,
)
from run_context import Variant1RunContext, bind_run_context
from work_fabric.scope import WorkScope
from work_fabric.service import WorkService


class FakeChatTransport:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(dict(payload))


@pytest.mark.asyncio
async def test_scoped_question_snapshot_contains_ordinary_and_goal_questions_and_fences_answers(tmp_path):
    work = WorkService.open(str(tmp_path/'questions.sqlite3'))
    ordinary = work.interactions.create(kind='clarification', prompt='A?',
        schema={'questions':[{'id':'q1','question':'A?'}]}, owner_kind='run', owner_id='run-a',
        scope=WorkScope(chat_id='chat-a'))
    goal = work.interactions.create(kind='goal_input', prompt='Goal?', owner_kind='goal', owner_id='goal-a',
                                    scope=WorkScope(chat_id='chat-a'))
    work.interactions.create(kind='clarification', prompt='B?', owner_kind='run', owner_id='run-b',
                             scope=WorkScope(chat_id='chat-b'))
    runtime = SimpleNamespace(work=work, goals=None)
    srv = SimpleNamespace(require_runtime=lambda:runtime)
    session = SimpleNamespace(viewed_session_id='chat-b', active=SimpleNamespace())
    transport = FakeChatTransport()
    await ws_dispatch.HANDLERS['clarification:list'](srv, transport, session, {'chat_id':'chat-a','request_id':'snapshot-a'})
    snapshot = transport.messages[-1]
    assert snapshot['type'] == 'clarification:snapshot' and snapshot['request_id'] == 'snapshot-a'
    assert {p['id'] for p in snapshot['pending']} == {ordinary.interaction_id, goal.interaction_id}
    assert session.viewed_session_id == 'chat-b'
    await ws_dispatch.HANDLERS['clarification:response'](srv, transport, session,
        {'chat_id':'chat-b', 'request_id':'wrong-owner', 'id':ordinary.interaction_id, 'answers':{'q1':'answer'}})
    assert work.interactions.get(ordinary.interaction_id).status == 'open'
    assert transport.messages[-2]['type'] == 'clarification:response:ack'
    assert transport.messages[-2]['status'] == 'stale'
    await ws_dispatch.HANDLERS['clarification:list'](srv, transport, session, {'chat_id':'empty','request_id':'empty'})
    assert transport.messages[-1]['pending'] == []


@pytest.mark.asyncio
async def test_goal_clarification_list_uses_viewed_chat_and_scoped_empty(
    tmp_path,
):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    transport = FakeChatTransport()
    runtime = SimpleNamespace(
        work=SimpleNamespace(interactions=work.interactions),
        goals=None,
    )
    server = SimpleNamespace(require_runtime=lambda: runtime)
    session = SimpleNamespace(
        active=SimpleNamespace(
            runtime_chat_id="chat-active",
            turn_session_id="chat-turn",
        ),
        viewed_session_id="chat-viewed",
    )

    await ws_dispatch.HANDLERS["clarification:list"](
        server, transport, session, {},
    )

    assert transport.messages == [{
        "type": "clarification:closed",
        "id": "",
        "kind": "goal_input",
        "chat_id": "chat-viewed",
        "status": "empty",
    }]


@pytest.mark.asyncio
async def test_goal_clarification_list_projects_matching_interaction(tmp_path):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    record = work.interactions.create(
        kind="goal_input",
        prompt="Choose a lane",
        owner_kind="goal",
        owner_id="goal-1",
        scope=WorkScope(chat_id="chat-a", goal_id="goal-1", step_id="choose"),
    )
    transport = FakeChatTransport()
    runtime = SimpleNamespace(
        work=SimpleNamespace(interactions=work.interactions),
        goals=None,
    )
    server = SimpleNamespace(require_runtime=lambda: runtime)
    session = SimpleNamespace(
        active=SimpleNamespace(runtime_chat_id="", turn_session_id=""),
        viewed_session_id="chat-a",
    )

    await ws_dispatch.HANDLERS["clarification:list"](
        server, transport, session, {},
    )

    assert transport.messages[0]["type"] == "clarification:request"
    assert transport.messages[0]["id"] == record.interaction_id
    assert transport.messages[0]["chat_id"] == "chat-a"


def test_agent_context_explicitly_inherits_chat_transport():
    transport = FakeChatTransport()
    parent = Variant1RunContext.create(
        source="chat",
        chat_session=object(),
        chat_transport=transport,
        metadata={"_server_bound_kind": "chat"},
    )
    state = new_run_state(source="chat", title="question", goal="question")

    with bind_run_context(parent):
        child = run_context_from_state(chat_task_default(), state, runtime=None)

    assert child.chat_transport is transport
    assert "websocket" not in child.metadata


@pytest.mark.asyncio
async def test_ask_user_round_trip_uses_typed_chat_transport(tmp_path):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    session = object()
    transport = FakeChatTransport()
    ctx = Variant1RunContext.create(
        source="chat",
        chat_session=session,
        chat_transport=transport,
        work_scope=WorkScope(chat_id="chat-clarification"),
    )
    args = {"questions": [{
        "question": "Which format should I use?",
        "header": "Format",
        "options": [
            {"label": "Markdown", "description": "Return a Markdown document."},
            {"label": "Plain text", "description": "Return an unformatted text file."},
        ],
    }]}

    with bind_run_context(ctx):
        pending = asyncio.create_task(tool_ask_user(work.interactions, args))
        for _ in range(20):
            await asyncio.sleep(0)
            request = next(
                (row for row in transport.messages if row.get("type") == "clarification:request"),
                None,
            )
            if request:
                break
        assert request is not None
        assert resolve_response(
            work.interactions,
            request["id"], {"q1": "Markdown"}, skipped=False,
        )
        result = await pending

    assert '"Which format should I use?" = "Markdown"' in result
    assert result.programmatic_value == {
        "interaction_id": request["id"], "status": "answered",
        "answers": {"q1": "Markdown"},
    }
    assert transport.messages[-1]["type"] == "clarification:closed"
    assert work.interactions.get(request["id"]).status == "answered"


@pytest.mark.asyncio
@pytest.mark.parametrize("status, answer, multiple, expected", [
    ("answered", "Maple", False, {"q1": "Maple"}),
    ("answered", 'Free text: "Maple", Cedar', False, {"q1": 'Free text: "Maple", Cedar'}),
    ("answered", ["Cedar", "Maple"], True, {"q1": ["Cedar", "Maple"]}),
    ("answered", None, False, {}),
    ("skipped", None, False, {}),
    ("timed_out", None, False, {}),
    ("cancelled", None, False, {}),
])
async def test_python_clarification_preserves_answer_data_and_terminal_status(
    tmp_path, status, answer, multiple, expected,
):
    work = WorkService.open(str(tmp_path / "typed-answers.sqlite3"))
    transport = FakeChatTransport()
    ctx = Variant1RunContext.create(
        source="chat", chat_session=object(), chat_transport=transport,
        work_scope=WorkScope(chat_id="chat-typed-answers"),
    )
    args = {"questions": [{
        "question": "Which label?", "header": "Label", "multiSelect": multiple,
        "options": [{"label": "Cedar", "description": "Use Cedar."},
                    {"label": "Maple", "description": "Use Maple."}],
    }]}
    with bind_run_context(ctx):
        pending = asyncio.create_task(tool_ask_user(work.interactions, args))
        try:
            for _ in range(20):
                await asyncio.sleep(0)
                request = next((m for m in transport.messages
                                if m.get("type") == "clarification:request"), None)
                if request:
                    break
            assert request is not None
            if status in {"answered", "skipped"}:
                assert resolve_response(
                    work.interactions, request["id"],
                    {"q1": answer} if answer is not None else {},
                    skipped=status == "skipped", chat_id="chat-typed-answers",
                )
            else:
                current = work.interactions.get(request["id"])
                work.interactions.resolve(
                    request["id"], status=status, expected_version=current.version,
                )
            result = await pending
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert result.programmatic_value == {
        "interaction_id": request["id"], "status": status, "answers": expected,
    }
    assert result.receipt_metadata["projection"] == "clarification-answer-v1"
    assert transport.messages[-1]["type"] == "clarification:closed"


def test_registered_ask_user_describes_the_python_answer_contract():
    from host_tool_surface import register_all

    registry = tools.ToolRegistry()
    runtime = SimpleNamespace(registry=registry, execution=object())
    register_all(SimpleNamespace(require_runtime=lambda: runtime))
    tool = registry.get("ask_user")
    assert tool.result_projection == "clarification-answer-v1"
    assert "result['answers']['q1']" in tool.description


def test_ask_user_accepts_binary_action_choice():
    args = [{
        "question": "Do you want me to copy this value to your clipboard?",
        "header": "Clipboard confirmation",
        "options": [
            {"label": "Proceed", "description": "Yes, copy it to the clipboard."},
            {"label": "Cancel", "description": "No, do not change the clipboard."},
        ],
    }]

    questions = normalize_questions(args)
    assert questions[0]["question"].startswith("Do you want me")


def test_ask_user_allows_optional_yes_no_preference():
    questions = normalize_questions([{
        "question": "Should the report include a chart?",
        "header": "Chart",
        "options": [
            {"label": "Yes", "description": "Include one concise chart."},
            {"label": "No", "description": "Keep the report text-only."},
        ],
    }])

    assert questions[0]["question"] == "Should the report include a chart?"


def test_goal_input_projects_as_freeform_inline_question(tmp_path):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    record = work.interactions.create(
        kind="goal_input",
        prompt="Which deployment lane should the goal use?",
        owner_kind="goal",
        owner_id="goal-1",
        scope=WorkScope(chat_id="chat-a", goal_id="goal-1", step_id="choose"),
        metadata={"title": "Goal input required"},
    )

    assert pending_goal_input(work.interactions, "chat-a") == record
    projected = interaction_request(record)
    assert projected["kind"] == "goal_input"
    assert projected["chat_id"] == "chat-a"
    assert projected["questions"][0]["options"] == []
