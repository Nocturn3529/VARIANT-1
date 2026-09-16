"""Provider termination and native-tool stream diagnostics."""

from __future__ import annotations

import pytest

from session_catalog.service import IPYTHON_PROVIDER_SPEC
from llm_router import LLMRouter
from llm_stream_diagnostics import StreamDiagnostics, counting_tool_sink
from tool_calling import ToolCallAccumulator
from tests.support.model_config import with_test_support


def test_counting_sink_preserves_calls_and_counts_wire_fragments():
    diagnostics = StreamDiagnostics()
    accumulator = ToolCallAccumulator()
    sink = counting_tool_sink(accumulator, diagnostics)

    sink.add_openai_delta([{
        "index": 0,
        "id": "call_1",
        "function": {"name": "read_file", "arguments": "{\"path\":"},
    }])
    sink.add_openai_delta([{
        "index": 0,
        "function": {"arguments": "\"README.txt\"}"},
    }])

    assert diagnostics.tool_deltas == 2
    assert accumulator.actions()[0]["args"] == {"path": "README.txt"}


@pytest.mark.asyncio
async def test_router_done_log_reports_finish_reason_and_tool_deltas(capsys):
    router = LLMRouter(with_test_support({"mode": "local"}), app_root=".")

    async def local_stream(*_args, tool_call_sink=None,
                           stream_diagnostics=None, **_kwargs):
        tool_call_sink.add_openai_delta([{
            "index": 0,
            "id": "call_1",
            "function": {"name": "ipython", "arguments": "{}"},
        }])
        stream_diagnostics.note_finish_reason("tool_calls")
        yield ""

    router._call_local = local_stream
    accumulator = ToolCallAccumulator()
    _ = [token async for token in router.stream(
        [{"role": "user", "content": "probe"}],
        tools=[IPYTHON_PROVIDER_SPEC],
        tool_call_sink=accumulator,
    )]

    output = capsys.readouterr().out
    assert "tool_deltas=1" in output
    assert "finish=tool_calls" in output
    assert accumulator.actions()[0]["tool"] == "ipython"
