from agent_types import ToolBatchResult
from assistant_turn import normalized_stop_reason


def test_terminal_stop_reasons_are_not_masked_by_tool_calls():
    assert normalized_stop_reason("length", has_tool_calls=True) == "length"
    assert normalized_stop_reason("error", has_tool_calls=True) == "error"
    assert normalized_stop_reason("aborted", has_tool_calls=True) == "aborted"
    assert normalized_stop_reason("stop", has_tool_calls=True) == "tool_use"


def test_tool_batch_result_preserves_call_bound_outcomes():
    result = ToolBatchResult(
        text="ok",
        executed=True,
        outcomes=[{"call_id": "c1", "result": "ok"}],
    )
    assert isinstance(result, ToolBatchResult)
    assert result.executed is True
    assert result.outcomes[0]["call_id"] == "c1"
