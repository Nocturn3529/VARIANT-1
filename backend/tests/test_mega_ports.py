"""Structured mega-ports: nested groups only (no flat adapter)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent_engine.task_ports import (
    LoopPorts,
    TaskLoopCorePorts,
    TaskSetupPorts,
    TaskTurnPorts,
    loop_ports_from_task_turn,
)
from agent_engine.shared_ports import AgentContextPorts
from chat_pipeline import (
    ChatCommandPorts,
    ChatIoPorts,
    ChatMemoryPorts,
    ChatPorts,
    ChatSessionPorts,
    ChatToolsPorts,
    ChatTtsPorts,
    ChatVisionPorts,
)


async def _anoop(*a, **k):
    return None


def _make_chat_ports() -> ChatPorts:
    async def anoop(*a, **k):
        return None

    ports = ChatPorts(
        io=ChatIoPorts(
            router=SimpleNamespace(name="r"),
            hub=SimpleNamespace(broadcast=anoop),
            sessions=SimpleNamespace(get_active=lambda: "s1"),
            emit=anoop,
        ),
        memory=ChatMemoryPorts(
            mem_query=anoop,
            mem_add=anoop,
            extract_and_store=anoop,
        ),
        vision=ChatVisionPorts(
            vision_state=lambda: (False, "single"),
        ),
        tools=ChatToolsPorts(
            prompt_context=lambda *a, **k: None,
            build_task_turn_ports=lambda ws, s: None,
            provider_specs=lambda session: [],
            runtime_prompt_block=lambda session, query="": "runtime",
            graph_revision=lambda session: "chat.ipython.v2",
            runtime_identity=lambda session: {
                "action_surface": "trusted-local.v1",
                "provider_tool_schema_revision": "ipython.portable.v6",
            },
        ),
        session=ChatSessionPorts(
            handle_chat=anoop,
            make_run_context=lambda *a, **k: None,
            snapshot_resume_state=lambda: (None, None),
            set_last_user_text=lambda t: None,
        ),
        tts=ChatTtsPorts(
            tts_enabled=lambda: False,
            tts_speed=lambda: 1.0,
        ),
    )
    return ports


def test_chat_ports_nested_groups():
    ports = _make_chat_ports()
    assert isinstance(ports.io, ChatIoPorts)
    assert isinstance(ports.tts, ChatTtsPorts)
    assert ports.io.router is not None
    assert ports.tts.tts_enabled() is False
    assert not hasattr(ChatPorts, "__getattr__")


def test_chat_ports_nested_construction():
    base = _make_chat_ports()
    sentinel = lambda: True
    ports = ChatPorts(
        io=base.io,
        memory=base.memory,
        vision=base.vision,
        tools=base.tools,
        session=base.session,
        tts=base.tts,
        commands=ChatCommandPorts(system_status=sentinel),
    )
    assert ports.commands.system_status is sentinel
    assert ports.io.hub is base.io.hub


def _make_task_turn_ports() -> TaskTurnPorts:
    async def astream(msgs, n, img):
        from assistant_turn import AssistantTurn
        return AssistantTurn(text="ok")

    async def arun(acts):
        return SimpleNamespace(cancelled=False, executed=True)

    async def aemit(*a, **k):
        return None

    async def acompress(msgs):
        return msgs

    return TaskTurnPorts(
        setup=TaskSetupPorts(
            build_tools_block=lambda e, s: "",
            tool_lines=lambda s: "",
            registry_get=lambda n: None,
            make_task=lambda g: SimpleNamespace(goal=g),
            new_run=lambda t, s: None,
            install_image_sink=lambda d: None,
            use_reasoning=lambda: False,
            current_model=lambda: "test",
        ),
        loop=TaskLoopCorePorts(
            stream=astream,
            run_actions=arun,
            emit=aemit,
            should_stop=lambda: False,
            clip=lambda s, n: s[:n],
            state_block=lambda t: "",
            drain_steering=lambda: None,
            drain_follow_up=lambda: None,
            record_active_input=lambda row, assistant: None,
        ),
        context=AgentContextPorts(
            approx_tokens=lambda m: 0,
            ctx_compress_threshold=lambda: 8000,
            compress_messages=acompress,
        ),
    )


def test_task_turn_ports_and_loop_projection():
    ports = _make_task_turn_ports()
    assert isinstance(ports.setup, TaskSetupPorts)
    assert isinstance(ports.loop, TaskLoopCorePorts)
    assert isinstance(ports.context, AgentContextPorts)

    async def compress(msgs):
        return msgs

    def disclose(*a):
        return None

    loop = loop_ports_from_task_turn(
        ports, compress=compress, progressive_disclose=disclose)
    assert isinstance(loop, LoopPorts)
    assert loop.stream is ports.loop.stream
    assert not hasattr(loop, "is_light")
    assert loop.compress is compress
