"""tool_runner.execute_tool_batch sequencing, pairing, and projections."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from observability import context_lineage
from run_context import Variant1RunContext, bind_run_context
from tool_core import ToolExecutionResult
from tool_calling import format_tool_result_messages
from tool_runner import ToolRunnerPorts, execute_tool_batch as _execute_tool_batch


class FakeTool:
    def __init__(self, name, delay=0.0, result=None, raise_exc=None):
        self.name = name
        self.delay = delay
        self.result = result if result is not None else f"{name} ok"
        self.raise_exc = raise_exc
        self.calls = 0

    async def run(self, args):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_exc:
            raise self.raise_exc
        return self.result


class CapturingTool(FakeTool):
    async def run(self, args):
        from desktop import service as desktop_service
        desktop_service.deliver_image("image-bytes")
        return await super().run(args)


class _RunnerBroker:
    """Explicit broker seam for runner-only tests; never a production bypass."""

    def __init__(self, runnable):
        self.tools = {}
        for row in runnable:
            tool = row.get("tool")
            if tool is not None:
                self.tools.setdefault(row["a"]["tool"], []).append(tool)

    def context_for_provider(self, **_fields):
        return SimpleNamespace()

    def reserve_provider_outer_call(self, **_fields):
        return {"decision": "execute"}

    def finish_provider_outer_call(self, _reservation, _outcome):
        return None

    async def invoke_name(self, name, args, _context):
        raw = await self.tools[name].pop(0).run(args)
        terminate = isinstance(raw, ToolExecutionResult) and raw.terminate
        value = raw.content if isinstance(raw, ToolExecutionResult) else raw
        receipt_id = f"test-receipt-{name}"
        return SimpleNamespace(
            ok=True,
            result_value=value,
            error=None,
            status="ok",
            effect=SimpleNamespace(attempted_at="test"),
            terminate=terminate,
            receipt_id=receipt_id,
            to_dict=lambda: {
                "receipt_id": receipt_id,
                "status": "ok",
                "terminate": terminate,
            },
        )


async def execute_tool_batch(runnable, **kwargs):
    return await _execute_tool_batch(
        runnable,
        broker=_RunnerBroker(runnable),
        **kwargs,
    )


def _runnable(name, tool, args=None, status="ok", call_id=""):
    action = {"tool": name, "args": args or {}}
    if call_id:
        action["id"] = call_id
    return {
        "a": action,
        "tool": tool,
        "status": status,
    }


def _ports(running_log=None):
    running_log = running_log if running_log is not None else []

    async def send_running(name, args, _call_id):
        running_log.append(name)

    async def emit(event, **fields):
        pass

    return ToolRunnerPorts(
        emit=emit,
        send_running=send_running,
        clip=lambda s, n: (s or "")[:n],
        max_result_chars=100000,
    ), running_log


@pytest.mark.asyncio
async def test_batch_terminates_only_when_every_call_requests_it():
    ports, _ = _ports()
    first = FakeTool("first", result=ToolExecutionResult("done", terminate=True))
    second = FakeTool("second", result=ToolExecutionResult("also done", terminate=True))

    batch = await execute_tool_batch(
        [_runnable("first", first), _runnable("second", second)],
        should_stop=None,
        ports=ports,
    )

    assert batch.terminate is True
    assert all(outcome["terminate"] for outcome in batch.outcomes)


@pytest.mark.asyncio
async def test_plain_or_invalid_outcome_prevents_batch_termination():
    ports, _ = _ports()
    terminating = FakeTool("finish", result=ToolExecutionResult("done", terminate=True))

    batch = await execute_tool_batch(
        [_runnable("finish", terminating), _runnable("plain", FakeTool("plain"))],
        should_stop=None,
        ports=ports,
    )
    invalid = await execute_tool_batch(
        [_runnable("finish", terminating), _runnable("bad", None, status="invalid_arguments")],
        should_stop=None,
        ports=ports,
    )

    assert batch.terminate is False
    assert invalid.terminate is False


@pytest.mark.asyncio
async def test_tool_capture_is_bound_to_exact_call_id():
    from desktop import service as desktop_service

    holder = {"image": None}
    token = desktop_service.install_image_sink(holder)
    try:
        tool = CapturingTool("computer", result="clicked")
        row = _runnable("computer", tool, {"action": "click"})
        row["a"]["id"] = "call_exact_42"
        ports, _ = _ports()
        await execute_tool_batch(
            [row],
            should_stop=None,
            ports=ports,
        )
    finally:
        desktop_service.reset_image_sink(token)

    assert holder["image"]["origin"] == "tool_result"
    assert holder["image"]["tool_call_ids"] == ["call_exact_42"]
    assert holder["image"]["tool_name"] == "computer"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_late_capture_from_completed_tool_binding_is_dropped():
    from desktop import service as desktop_service

    holder = {"image": None}
    sink_token = desktop_service.install_image_sink(holder)
    release = asyncio.Event()

    async def delayed_capture():
        await release.wait()
        desktop_service.deliver_image("late-image")

    provenance_token = desktop_service.bind_image_provenance(
        "call_already_finished", "computer",
    )
    task = asyncio.create_task(delayed_capture())
    desktop_service.reset_image_provenance(provenance_token)
    release.set()
    await task
    desktop_service.reset_image_sink(sink_token)

    assert holder["image"] is None
    assert "_active_image_binding" not in holder


@pytest.mark.asyncio
async def test_tools_run_sequentially_and_in_order():
    """A model-authored action batch runs one tool at a time, in order."""
    order = []

    class OrderedTool(FakeTool):
        async def run(self, args):
            order.append(f"start:{self.name}")
            out = await super().run(args)
            order.append(f"end:{self.name}")
            return out

    tool_a = OrderedTool("read_file", delay=0.02, result="file a")
    tool_b = OrderedTool("read_file", delay=0.02, result="file b")
    runnable = [_runnable("read_file", tool_a), _runnable("read_file", tool_b)]
    ports, _ = _ports()

    br = await execute_tool_batch(runnable, should_stop=None, ports=ports)

    assert order == ["start:read_file", "end:read_file", "start:read_file", "end:read_file"], \
        "ordinary tools must not overlap"
    assert br.text.index("file a") < br.text.index("file b")


@pytest.mark.asyncio
async def test_tool_projection_records_lengths_without_result_values():
    secret = "SENTINEL-private-tool-output-" * 100
    tool = FakeTool("read_file", result=secret)
    ports, _ = _ports()
    ports.max_result_chars = 20
    receipt = context_lineage.new_receipt("main_chat_step")
    ctx = Variant1RunContext.create(
        source="chat", run_id="tool-lineage",
        model_input_receipt=receipt,
    )

    with bind_run_context(ctx):
        await execute_tool_batch(
            [_runnable("read_file", tool)],
            should_stop=None,
            ports=ports,
        )

    assert receipt["items"][-1]["kind"] == "tool_observation"
    assert receipt["items"][-1]["chars_before"] == len(secret)
    assert receipt["items"][-1]["chars_after"] < len(secret)
    assert receipt["transforms"][-1]["kind"] == "tool_output_clipped"
    assert secret not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_failed_tool_preserves_diagnostic_tail_for_model_and_summary():
    diagnostic = "Set-Content : UNIQUE positional parameter failure"
    failure = (
        "$ " + ("long-command " * 700) + "\n"
        "shell: PowerShell\n"
        "cwd: C:\\workspace\n"
        "exit: 1\n"
        "duration_s: 0.1\n"
        "--- stderr ---\n"
        f"{diagnostic}\n"
        "FullyQualifiedErrorId : PositionalParameterNotFound"
    )
    tool = FakeTool("run_command", raise_exc=RuntimeError(failure))
    ports, _ = _ports()

    br = await execute_tool_batch(
        [_runnable("run_command", tool)],
        should_stop=None,
        ports=ports,
    )

    assert br.had_error is True
    assert tool.calls == 1
    assert diagnostic in br.text
    assert br.outcomes[0]["model_result"] == failure
    assert br.outcomes[0]["truncated"] is False

    followup = format_tool_result_messages(
        [{"tool": "run_command", "args": {}, "id": "call_error"}],
        outcomes=br.outcomes,
    )
    assert followup[0]["content"] == failure
    assert diagnostic in followup[0]["content"]


@pytest.mark.asyncio
async def test_cancellation_synthesizes_one_result_for_every_unstarted_call():
    first = FakeTool("read_file")
    second = FakeTool("run_command")
    runnable = [
        _runnable("read_file", first, call_id="call_a"),
        _runnable("run_command", second, call_id="call_b"),
    ]
    events = []
    ports, _ = _ports()

    async def emit(event, **fields):
        events.append((event, fields))

    ports.emit = emit
    br = await execute_tool_batch(
        runnable,
        should_stop=lambda: True,
        ports=ports,
    )

    assert br.cancelled is True
    assert br.executed is False
    assert first.calls == second.calls == 0
    assert [row["call_id"] for row in br.outcomes] == ["call_a", "call_b"]
    assert [row["status"] for row in br.outcomes] == ["cancelled", "cancelled"]
    assert all(row["executed"] is False for row in br.outcomes)
    assert [fields["call_id"] for _, fields in events] == ["call_a", "call_b"]

    followup = format_tool_result_messages(
        [row["a"] for row in runnable],
        outcomes=br.outcomes,
    )
    assert [message["tool_call_id"] for message in followup] == [
        "call_a", "call_b",
    ]
    assert all(message["content"] == "cancelled before execution"
               for message in followup)


@pytest.mark.asyncio
async def test_cancellation_after_first_call_preserves_order_and_pairing():
    stopped = False

    class StopAfterRun(FakeTool):
        async def run(self, args):
            nonlocal stopped
            result = await super().run(args)
            stopped = True
            return result

    first = StopAfterRun("read_file", result="first result")
    second = FakeTool("run_command", result="must not run")
    runnable = [
        _runnable("read_file", first, call_id="call_first"),
        _runnable("run_command", second, call_id="call_second"),
    ]
    ports, _ = _ports()

    br = await execute_tool_batch(
        runnable,
        should_stop=lambda: stopped,
        ports=ports,
    )

    assert first.calls == 1
    assert second.calls == 0
    assert br.executed is True
    assert br.cancelled is True
    assert [row["call_id"] for row in br.outcomes] == [
        "call_first", "call_second",
    ]
    assert [row["status"] for row in br.outcomes] == ["ok", "cancelled"]


@pytest.mark.asyncio
async def test_validation_and_unavailable_tools_each_return_one_bound_result():
    invalid = _runnable(
        "read_file", FakeTool("read_file"), status="invalid_arguments",
        call_id="call_invalid",
    )
    invalid["validation_error"] = "path must be a string"
    unavailable = _runnable(
        "missing_tool", None, status="unavailable", call_id="call_missing",
    )
    ports, _ = _ports()

    br = await execute_tool_batch(
        [invalid, unavailable],
        should_stop=None,
        ports=ports,
    )

    assert br.had_error is True
    assert br.executed is False
    assert [row["call_id"] for row in br.outcomes] == [
        "call_invalid", "call_missing",
    ]
    assert [row["status"] for row in br.outcomes] == [
        "invalid_arguments", "unavailable",
    ]
    assert len(br.outcomes) == 2
