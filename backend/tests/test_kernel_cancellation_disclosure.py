from dataclasses import replace

from kernel_runtime.contracts import KernelExecutionResult
from kernel_runtime.output import CellOutput


def test_cancellation_discloses_only_an_actually_retained_generation():
    result = KernelExecutionResult(
        execution_id="interrupted-cell", chat_id="interrupted-chat", generation=7,
        status="cancelled", output=CellOutput(), error_code="kernel_cell_cancelled",
        error_message="Cell cancelled by the host.",
    )
    rendered = result.model_observation()
    assert "ERROR kernel_cell_cancelled" in rendered
    assert "CPython generation 7 is still live" in rendered
    assert "not rolled back" in rendered
    assert "verify partial external effects" in rendered

    restarted = replace(result, generation=8, hard_restarted=True).model_observation()
    assert "Runtime restarted" in restarted
    assert "still live" not in restarted
    assert "still live" not in replace(result, generation=0).model_observation()
    assert "still live" not in replace(result, status="ok", error_code="").model_observation()
    assert "still live" not in replace(result, status="error", error_code="python_exception").model_observation()
