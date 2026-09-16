from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant_turn import AssistantTurn
from chat_session import ConnectionSession
from host_ports import build_task_turn_ports, tool_runner_ports


def _host_with_stream(tokens):
    async def stream(_messages, **_kwargs):
        for token in tokens:
            yield token

    async def compress_messages(messages, **_kwargs):
        return messages

    chat = SimpleNamespace(
        approx_tokens=lambda _messages: 0,
        ctx_compress_threshold=lambda: 1_000,
        compress_messages=compress_messages,
        count_prompt_tokens=AsyncMock(return_value=0),
        is_desktop_action=lambda _actions: False,
    )
    runtime = SimpleNamespace(
        chat=chat,
        registry=SimpleNamespace(get=lambda _name: None),
        actions=SimpleNamespace(run_interactive=AsyncMock()),
        session_runtimes=SimpleNamespace(
            claim_input=lambda *_args, **_kwargs: None,
            record_input_delivery=lambda *_args, **_kwargs: None,
        ),
    )
    return SimpleNamespace(
        router=SimpleNamespace(
            stream=stream,
            mode="local",
            reasoning=False,
            projection_budget_tokens=lambda: 4_096,
        ),
        require_runtime=lambda: runtime,
        tools_prompt_block=lambda _enabled, _specs: "",
        tool_lines=lambda _specs: "",
        Task=lambda **kwargs: SimpleNamespace(**kwargs),
        new_run=lambda _goal, _model: None,
        desktop_control=SimpleNamespace(
            clear_target=lambda: None,
            install_image_sink=lambda _run: None,
            consume_ui_change=lambda: None,
        ),
        active_model_name=lambda: "test-model",
        emit_activity=AsyncMock(),
        hub=SimpleNamespace(broadcast=AsyncMock()),
        clip=lambda text, _limit: text,
        state_block=lambda _state: "",
    )


@pytest.mark.parametrize('effective', [False, True, None])
def test_continuation_uses_effective_authority_and_admitted_receipt_owner(effective):
    host = _host_with_stream([])
    runtime = host.require_runtime()
    session = ConnectionSession()
    session.active.runtime_chat_id = 'runtime-chat'
    session.active.turn_session_id = 'durable-chat'
    session.viewed_session_id = 'unrelated-view'
    seen = []
    def authority(chat_id):
        assert chat_id == 'runtime-chat'
        if effective is None:
            raise RuntimeError('authority temporarily unavailable')
        return {'write_enabled': True, 'operator_allowed': effective,
                'effective_write_enabled': effective}
    def continuation(chat_id, **kwargs):
        seen.append((chat_id, kwargs))
        return 'native kernel note'
    def receipt(chat_id):
        assert chat_id == 'durable-chat'  # Same owner used by chat_pipeline's receipt writer.
        return {'run_id': 'previous', 'status': 'completed', 'tool_calls': 0}
    runtime.catalog = SimpleNamespace(mutation=SimpleNamespace(authority_status=authority))
    runtime.kernel = SimpleNamespace(continuation_context=continuation)
    runtime.sessions = SimpleNamespace(get_last_run_receipt=receipt)
    ports = build_task_turn_ports(host, SimpleNamespace(), session)
    note = ports.setup.continuation_context('Continue pb10_state')
    assert seen == [('runtime-chat', {'current_user_text': 'Continue pb10_state',
                                     'mutation_enabled': effective is True})]
    assert 'native kernel note' in note and 'Host previous-run execution' in note
    assert 'unrelated-view' not in note


@pytest.mark.asyncio
async def test_task_stream_sends_live_tokens_only_to_the_owner_socket():
    owner = SimpleNamespace(send_json=AsyncMock())
    session = ConnectionSession()
    session.active.turn_client_id = "deck-react-owner"
    session.active.turn_source = "chat"
    host = _host_with_stream(["Hel", "", "lo"])

    turn = await build_task_turn_ports(host, owner, session).loop.stream(
        [{"role": "user", "content": "Hi"}], 64, None
    )

    assert isinstance(turn, AssistantTurn)
    assert turn.text == "Hello"
    assert [call.args[0] for call in owner.send_json.await_args_list] == [
        {
            "type": "token",
            "token": "Hel",
            "client_id": "deck-react-owner",
            "source": "chat",
        },
        {
            "type": "token",
            "token": "lo",
            "client_id": "deck-react-owner",
            "source": "chat",
        },
    ]
    host.hub.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_stream_propagates_owner_socket_delivery_failures():
    owner = SimpleNamespace(
        send_json=AsyncMock(side_effect=RuntimeError("owner disconnected"))
    )
    session = ConnectionSession()
    host = _host_with_stream(["token"])

    with pytest.raises(RuntimeError, match="owner disconnected"):
        await build_task_turn_ports(host, owner, session).loop.stream([], 16, None)


@pytest.mark.asyncio
async def test_targeted_tool_start_carries_exact_call_identity():
    owner = SimpleNamespace(send_json=AsyncMock())
    session = ConnectionSession()
    session.active.turn_client_id = "deck-react-owner"
    session.active.turn_source = "chat"
    session.active.turn_session_id = "chat-one"
    host = _host_with_stream([])

    await tool_runner_ports(host, owner, session).send_running(
        "read_file", {"path": "demo.txt"}, "call-exact",
    )

    owner.send_json.assert_awaited_once_with({
        "type": "tool:activity",
        "event": "tool:start",
        "tool": "read_file",
        "call_id": "call-exact",
        "status": "running",
        "args_preview": '{"path": "demo.txt"}',
        "text": "Running read_file…",
        "client_id": "deck-react-owner",
        "source": "chat",
        "session_id": "chat-one",
    })
