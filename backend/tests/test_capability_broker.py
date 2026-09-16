"""Capability broker contract tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from artifacts.store import ContentAddressedArtifactStore
from capability_broker import (
    CapabilityBroker,
    CapabilityCall,
    InvocationContext,
)
from tool_runner import ToolRunnerPorts, execute_tool_batch
from tool_core import ToolError, ToolProjectionResult
from kernel_runtime.contracts import KernelExecutionError, KernelExecutionResult
from kernel_runtime.output import CellOutput
from tools import Tool, ToolRegistry
from run_context import Variant1RunContext, bind_run_context
from session_runtime import RuntimeIdentity, SessionRuntimeRepository


TEST_RELEASE = "astb.test.release.v1"


class _RuntimeRegistry:
    def ensure_runtime(self, chat_id):
        return type("Record", (), {
            "chat_id": str(chat_id),
            "identity": RuntimeIdentity(
                catalog_release_id=TEST_RELEASE,
                environment_digest="test-environment",
                mount_revision=1,
            ),
            "kernel_generation": 1,
        })()


def _context(*, nested_call_id: str = "nested-1", **overrides) -> InvocationContext:
    values = {
        "chat_id": "chat-1",
        "run_id": "run-1",
        "outer_tool_call_id": "outer-1",
        "cell_execution_id": "cell-1",
        "nested_call_id": nested_call_id,
        "catalog_release_id": TEST_RELEASE,
        "surface": "ipython",
    }
    values.update(overrides)
    return InvocationContext(**values)


def _broker(tmp_path, *registered: Tool, inline_result_bytes: int = 64 * 1024):
    registry = ToolRegistry()
    for tool in registered:
        registry.register(tool)
    enabled = {tool.name for tool in registered}
    store = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    receipts = []
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=_RuntimeRegistry(),
        enabled_resolver=lambda: set(enabled),
        artifact_store=store,
        inline_result_bytes=inline_result_bytes,
    )
    broker.register_receipt_sink(receipts.append, required=True)
    return broker, registry, store, receipts


def _echo_tool(calls: list[dict] | None = None) -> Tool:
    async def handler(args):
        if calls is not None:
            calls.append(dict(args))
        return f"echo:{args['value']}"

    return Tool(
        "echo",
        "Echo one value.",
        handler,
        params={"value": {"type": "string", "required": True}},
        effect_class="pure",
    )


def test_capability_identity_is_stable_and_not_provider_disclosed(tmp_path):
    tool = _echo_tool()
    broker, _, _, _ = _broker(tmp_path, tool)

    first = broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE)
    second = broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE)

    assert first == second
    assert first.opaque_id.startswith("cap_")
    assert first.schema_revision.startswith("variant1.schema.")
    assert first.handler_revision.startswith("variant1.handler.")
    assert "capability_id" not in tool.spec()
    assert "schema_revision" not in tool.spec()


@pytest.mark.asyncio
async def test_astb_gets_programmatic_value_while_provider_keeps_display(tmp_path):
    async def projected(_args):
        return ToolProjectionResult(
            "C:/x.txt (lines 1-1 of 1):\nVALUE\n",
            programmatic_value="VALUE\n",
            receipt_metadata={"projection": "file-content-v1", "path": "C:/x.txt"},
        )

    tool = Tool("projected_read", "read", projected, effect_class="read")
    broker, _, _, _ = _broker(tmp_path, tool)
    ref = broker.ref_for_name(
        "projected_read", catalog_release_id=TEST_RELEASE
    )
    cell = await broker.invoke(
        ref, {}, _context(nested_call_id="cell")
    )
    provider = await broker.invoke(
        ref, {}, _context(
            nested_call_id="provider", surface="provider"
        )
    )

    assert cell.result_value == "VALUE\n"
    assert provider.result_value.startswith("C:/x.txt (lines 1-1 of 1)")
    assert cell.result_metadata == {
        "projection": "file-content-v1", "path": "C:/x.txt",
    }
    assert cell.to_dict()["result_metadata"]["path"] == "C:/x.txt"


@pytest.mark.asyncio
async def test_success_is_attributed_persisted_once_and_deduplicated(tmp_path):
    calls: list[dict] = []
    tool = _echo_tool(calls)
    broker, _, _, persisted = _broker(tmp_path, tool)
    ref = broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE)
    context = _context()

    first = await broker.invoke(ref, {"value": "hello"}, context)
    replay = await broker.invoke(ref, {"value": "hello"}, context)

    assert first.ok is True
    assert first.result_value == "echo:hello"
    assert first.attribution["outer_tool_call_id"] == "outer-1"
    assert first.attribution["nested_call_id"] == "nested-1"
    assert "allowed_capability_ids" not in first.attribution
    assert "capability_grant_revision" not in first.attribution
    assert "policy_or_approval_ref" not in first.attribution
    assert replay.receipt_id == first.receipt_id
    assert replay.deduplicated is True
    assert calls == [{"value": "hello"}]
    assert len(persisted) == 1
    assert persisted[0].receipt_id == first.receipt_id
    assert "result_value" not in persisted[0].to_dict()


@pytest.mark.asyncio
async def test_conflicting_duplicate_never_reexecutes(tmp_path):
    calls: list[dict] = []
    tool = _echo_tool(calls)
    broker, _, _, _ = _broker(tmp_path, tool)
    ref = broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE)
    context = _context()

    first = await broker.invoke(ref, {"value": "one"}, context)
    conflict = await broker.invoke(ref, {"value": "two"}, context)

    assert first.ok is True
    assert conflict.ok is False
    assert conflict.error.code == "duplicate_nested_call_conflict"
    assert conflict.error.may_have_applied is False
    assert calls == [{"value": "one"}]


@pytest.mark.asyncio
async def test_duplicate_scope_isolated_by_durable_chat(tmp_path):
    calls: list[dict] = []
    tool = _echo_tool(calls)
    broker, _, _, _ = _broker(tmp_path, tool)
    ref = broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE)

    first = await broker.invoke(
        ref, {"value": "one"}, _context(nested_call_id="shared-id")
    )
    second = await broker.invoke(
        ref,
        {"value": "two"},
        _context(chat_id="chat-2", nested_call_id="shared-id"),
    )

    assert first.ok and second.ok
    assert first.receipt_id != second.receipt_id
    assert calls == [{"value": "one"}, {"value": "two"}]


@pytest.mark.asyncio
async def test_validation_fails_before_handler_dispatch(tmp_path):
    calls: list[dict] = []
    tool = _echo_tool(calls)
    broker, _, _, _ = _broker(tmp_path, tool)
    ref = broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE)

    receipt = await broker.invoke(ref, {"unexpected": "value"}, _context())

    assert receipt.status == "error"
    assert receipt.error.code == "invalid_arguments"
    assert receipt.error.may_have_applied is False
    assert receipt.effect.attempted_at is None or receipt.effect.attempted_at == ""
    assert calls == []


@pytest.mark.asyncio
async def test_deadline_distinguishes_safe_retry_from_ambiguous_effect(tmp_path):
    async def slow(_args):
        await asyncio.sleep(0.1)
        return "late"

    read = Tool("slow_read", "read", slow, params={}, effect_class="read")
    write = Tool("slow_write", "write", slow, params={}, effect_class="write")
    broker, _, _, _ = _broker(tmp_path, read, write)

    read_receipt = await broker.invoke(
        broker.ref_for_name("slow_read", catalog_release_id=TEST_RELEASE),
        {},
        _context(nested_call_id="read-timeout", deadline_ms=5),
    )
    write_receipt = await broker.invoke(
        broker.ref_for_name("slow_write", catalog_release_id=TEST_RELEASE),
        {},
        _context(nested_call_id="write-timeout", deadline_ms=5),
    )

    assert read_receipt.status == "timed_out"
    assert read_receipt.error.retryable is True
    assert read_receipt.error.may_have_applied is False
    assert write_receipt.status == "needs_reconciliation"
    assert write_receipt.error.retryable is False
    assert write_receipt.error.may_have_applied is True
    assert read_receipt.effect.observed_at and write_receipt.effect.observed_at


@pytest.mark.asyncio
async def test_cancelled_before_start_is_not_marked_attempted(tmp_path):
    calls: list[dict] = []
    tool = _echo_tool(calls)
    broker, _, _, _ = _broker(tmp_path, tool)

    receipt = await broker.invoke(
        broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE),
        {"value": "never"},
        _context(cancellation=lambda: True),
    )

    assert receipt.status == "cancelled_before_start"
    assert receipt.error.cause_class == "unknown"  # A boolean predicate does not identify the user.
    assert receipt.error.may_have_applied is False
    assert not receipt.effect.attempted_at
    assert calls == []


@pytest.mark.asyncio
async def test_broken_cancellation_authority_is_typed_and_fences_dispatch(tmp_path):
    calls: list[dict] = []
    tool = _echo_tool(calls)
    broker, _, _, _ = _broker(tmp_path, tool)

    def broken_authority():
        raise RuntimeError("cancellation lease disappeared")

    receipt = await broker.invoke(
        broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE),
        {"value": "never"},
        _context(cancellation=broken_authority),
    )

    assert receipt.status == "cancelled_before_start"
    assert receipt.error.code == "cancellation_authority_failed"
    assert "lease disappeared" in receipt.error.message
    assert receipt.error.may_have_applied is False
    assert calls == []


@pytest.mark.asyncio
async def test_user_disabled_facility_is_not_reported_as_model_failure(tmp_path):
    async def disabled(_args):
        raise ToolError(
            "Desktop control is off.\n[desktop_error:TOOL_UNAVAILABLE]"
        )

    tool = Tool(
        "computer",
        "desktop",
        disabled,
        params={},
        effect_class="external_side_effect",
    )
    broker, _, _, _ = _broker(tmp_path, tool)
    receipt = await broker.invoke(
        broker.ref_for_name("computer", catalog_release_id=TEST_RELEASE),
        {},
        _context(),
    )

    assert receipt.error.code == "facility_disabled_by_user"
    assert receipt.error.cause_class == "user"
    assert receipt.error.may_have_applied is False


@pytest.mark.asyncio
async def test_python_exception_crosses_broker_as_model_failure_not_harness(tmp_path):
    async def failed_cell(_args):
        raise KernelExecutionError(KernelExecutionResult(
            execution_id="exec-test",
            chat_id="chat-1",
            generation=1,
            status="error",
            output=CellOutput(
                error_name="AssertionError",
                error_value="expected 3, got 5",
                traceback=["Traceback", "AssertionError: expected 3, got 5"],
            ),
            error_code="python_exception",
            error_message="AssertionError: expected 3, got 5",
        ))

    tool = Tool("ipython", "python", failed_cell, params={}, effect_class="write")
    broker, _, _, _ = _broker(tmp_path, tool)
    receipt = await broker.invoke(
        broker.ref_for_name("ipython", catalog_release_id=TEST_RELEASE),
        {},
        _context(),
    )

    assert receipt.error.code == "python_exception"
    assert receipt.error.cause_class == "model"
    assert "ERROR python_exception" in receipt.error.message


@pytest.mark.asyncio
async def test_parent_task_cancellation_finalizes_the_dispatched_receipt(tmp_path):
    started = asyncio.Event()

    async def effect(_args):
        started.set()
        await asyncio.Event().wait()

    tool = Tool("cancelled_effect", "effect", effect, params={}, effect_class="write")
    broker, _, _, _ = _broker(tmp_path, tool)
    receipts = []
    broker.register_receipt_sink(receipts.append, required=True)
    ref = broker.ref_for_name(
        "cancelled_effect", catalog_release_id=TEST_RELEASE
    )
    context = _context()
    invocation = asyncio.create_task(broker.invoke(ref, {}, context))
    await started.wait()

    invocation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.status == "needs_reconciliation"
    assert receipt.error.code == "broker_parent_cancelled_after_dispatch"
    assert receipt.error.cause_class == "unknown"
    assert receipt.error.may_have_applied is True
    replay = await broker.invoke(ref, {}, context)
    assert replay.deduplicated is True
    assert replay.receipt_id == receipt.receipt_id


@pytest.mark.asyncio
@pytest.mark.parametrize("effect_class", ["write", "read"])
@pytest.mark.parametrize("payload", [b"binary", "large result" * 200])
async def test_projection_storage_failure_finalizes_and_replays_effect_truth(
    tmp_path, monkeypatch, effect_class, payload,
):
    calls = []

    async def handler(_args):
        calls.append("applied")
        return payload

    tool = Tool("projection_failure", "test", handler, effect_class=effect_class)
    broker, _, store, persisted = _broker(tmp_path, tool, inline_result_bytes=1024)

    def fail_storage(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "put_bytes", fail_storage)
    ref = broker.ref_for_name(tool.name, catalog_release_id=TEST_RELEASE)
    receipt = await broker.invoke(ref, {}, _context())
    replay = await broker.invoke(ref, {}, _context())

    assert receipt.status == ("needs_reconciliation" if effect_class == "write" else "error")
    assert receipt.error.code == "capability_result_projection_failed"
    assert receipt.error.cause_class == "harness"
    assert receipt.error.may_have_applied is (effect_class == "write")
    assert receipt.error.retryable is (effect_class == "read")
    assert receipt.effect.attempted_at
    assert receipt.effect.observed_at
    assert persisted == [receipt]
    assert replay.deduplicated
    assert replay.receipt_id == receipt.receipt_id
    assert replay.error == receipt.error
    assert calls == ["applied"]


@pytest.mark.asyncio
async def test_terminal_cache_pressure_preserves_active_admission(tmp_path):
    started = asyncio.Event()
    finish = asyncio.Event()
    effects = []

    async def slow_effect(_args):
        effects.append("applied")
        started.set()
        await finish.wait()
        return "finished"

    effect = Tool("slow_effect", "test", slow_effect, effect_class="write")
    broker, _, _, _ = _broker(tmp_path, effect, _echo_tool())
    broker.dedupe_capacity = 128
    effect_ref = broker.ref_for_name(effect.name, catalog_release_id=TEST_RELEASE)
    echo_ref = broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE)
    context = _context(nested_call_id="slow")
    invocation = asyncio.create_task(broker.invoke(effect_ref, {}, context))
    try:
        await started.wait()
        for index in range(broker.dedupe_capacity + 1):
            await broker.invoke(
                echo_ref, {"value": "fast"}, _context(nested_call_id=f"fast-{index}"),
            )
        replay = await asyncio.wait_for(broker.invoke(effect_ref, {}, context), timeout=1)
        assert replay.error.code == "duplicate_nested_call_in_flight"
        assert effects == ["applied"]
        assert len(broker.receipts(limit=1000)) == broker.dedupe_capacity
    finally:
        finish.set()
        await invocation
    replay = await broker.invoke(effect_ref, {}, context)
    assert replay.deduplicated
    assert replay.result_value == "finished"
    assert effects == ["applied"]


@pytest.mark.asyncio
async def test_large_result_is_spilled_without_losing_exact_process_value(tmp_path):
    payload = "0123456789" * 300

    async def large(_args):
        return payload

    tool = Tool("large_read", "large", large, params={}, effect_class="read")
    broker, _, store, _ = _broker(tmp_path, tool, inline_result_bytes=1024)

    receipt = await broker.invoke(
        broker.ref_for_name("large_read", catalog_release_id=TEST_RELEASE),
        {}, _context()
    )

    assert receipt.ok is True
    assert receipt.result_value == payload
    assert len(receipt.artifact_refs) == 1
    assert store.read_bytes(receipt.artifact_refs[0].ref).decode("utf-8") == payload
    assert receipt.content_blocks[0].type == "artifact_ref"
    assert receipt.truncation.dropped_bytes > 0
    assert payload not in json.dumps(receipt.to_dict())


@pytest.mark.asyncio
async def test_read_fanout_is_bounded_parallel_and_returns_input_order(tmp_path):
    active = 0
    peak = 0

    async def read(args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(float(args["delay"]))
            return args["label"]
        finally:
            active -= 1

    tool = Tool(
        "parallel_read",
        "read",
        read,
        params={
            "label": {"type": "string", "required": True},
            "delay": {"type": "number", "required": True},
        },
        effect_class="read",
        parallel_safe=True,
    )
    broker, _, _, _ = _broker(tmp_path, tool)
    ref = broker.ref_for_name("parallel_read", catalog_release_id=TEST_RELEASE)
    calls = [
        CapabilityCall(ref, {"label": "first", "delay": 0.04}, "fan-1"),
        CapabilityCall(ref, {"label": "second", "delay": 0.01}, "fan-2"),
        CapabilityCall(ref, {"label": "third", "delay": 0.02}, "fan-3"),
    ]

    receipts = await broker.invoke_many(calls, _context(), max_concurrency=2)

    assert [receipt.result_value for receipt in receipts] == ["first", "second", "third"]
    assert peak == 2


@pytest.mark.asyncio
async def test_effectful_batch_is_forced_serial(tmp_path):
    active = 0
    peak = 0

    async def write(args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return args["label"]
        finally:
            active -= 1

    tool = Tool(
        "serial_write",
        "write",
        write,
        params={"label": {"type": "string", "required": True}},
        effect_class="write",
    )
    broker, _, _, _ = _broker(tmp_path, tool)
    ref = broker.ref_for_name("serial_write", catalog_release_id=TEST_RELEASE)
    calls = [
        CapabilityCall(ref, {"label": str(index)}, f"write-{index}")
        for index in range(3)
    ]

    receipts = await broker.invoke_many(calls, _context(), max_concurrency=8)

    assert [receipt.result_value for receipt in receipts] == ["0", "1", "2"]
    assert peak == 1


@pytest.mark.asyncio
async def test_desktop_capability_uses_process_lock_once(tmp_path):
    lock = asyncio.Lock()
    observed = []

    async def desktop(_args):
        observed.append(lock.locked())
        return "clicked"

    tool = Tool(
        "desktop_click",
        "desktop",
        desktop,
        params={},
        effect_class="external_side_effect",
        touches_desktop=True,
    )
    registry = ToolRegistry()
    registry.register(tool)
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=_RuntimeRegistry(),
        enabled_resolver=lambda: {"desktop_click"},
        desktop_action_lock=lock,
    )

    first = await broker.invoke(
        broker.ref_for_name("desktop_click", catalog_release_id=TEST_RELEASE),
        {}, _context(nested_call_id="lock-1")
    )
    async with lock:
        second = await broker.invoke(
            broker.ref_for_name("desktop_click", catalog_release_id=TEST_RELEASE),
            {},
            _context(nested_call_id="lock-2", desktop_lock_held=True),
        )

    assert first.ok and second.ok
    assert observed == [True, True]
    assert not lock.locked()


@pytest.mark.asyncio
async def test_stale_capability_revision_fails_closed(tmp_path):
    tool = _echo_tool()
    broker, _, _, _ = _broker(tmp_path, tool)
    ref = broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE)

    stale = await broker.invoke(
        replace(ref, handler_revision="obsolete"),
        {"value": "x"},
        _context(nested_call_id="stale"),
    )
    assert stale.error.code == "capability_resolution_error"
    assert "stale capability reference" in stale.error.message


@pytest.mark.asyncio
async def test_provider_batch_keeps_result_and_adds_release_receipt(tmp_path):
    async def handler(_args):
        return "provider-result"

    tool = Tool("ipython", "read", handler, params={}, effect_class="read")
    broker, _, _, _ = _broker(tmp_path, tool)

    async def emit(_event, **_fields):
        return None

    async def running(_name, _args, _call_id):
        return None

    ports = ToolRunnerPorts(
        emit=emit,
        send_running=running,
        clip=lambda value, limit: str(value)[:limit],
        max_result_chars=1000,
    )
    batch = await execute_tool_batch(
        [{
            "a": {"tool": "ipython", "args": {}, "id": "provider-call-7"},
            "tool": tool,
            "status": "ok",
        }],
        should_stop=None,
        ports=ports,
        broker=broker,
    )

    assert batch.text == "[ipython] provider-result"
    assert len(batch.outcomes) == 1
    assert batch.outcomes[0]["model_result"] == "provider-result"
    assert batch.outcomes[0]["call_id"] == "provider-call-7"
    assert batch.outcomes[0]["capability_status"] == "ok"
    assert len(batch.receipts) == 1
    assert batch.receipts[0]["attribution"]["nested_call_id"] == (
        "provider:provider-call-7"
    )
    assert batch.receipts[0]["attribution"]["catalog_release_id"] == TEST_RELEASE


def _runner_ports():
    async def emit(_event, **_fields):
        return None

    async def running(_name, _args, _call_id):
        return None

    return ToolRunnerPorts(
        emit=emit,
        send_running=running,
        clip=lambda value, limit: str(value)[:limit],
        max_result_chars=1000,
    )


@pytest.mark.asyncio
async def test_provider_outer_call_terminal_result_replays_across_broker_restart(tmp_path):
    calls = []

    async def write(args):
        calls.append(dict(args))
        return "applied-once"

    tool = Tool(
        "ipython", "write", write,
        params={"value": {"type": "string", "required": True}},
        effect_class="external_side_effect",
    )
    registry = ToolRegistry(); registry.register(tool)
    repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
    repository.ensure_runtime("chat-outer", RuntimeIdentity())

    def broker_for(_name):
        return CapabilityBroker(
            registry=registry,
            runtime_registry=_RuntimeRegistry(),
            enabled_resolver=lambda: {"ipython"},
            outer_call_repository=repository,
        )

    runnable = [{
        "a": {
            "tool": "ipython", "args": {"value": "x"},
            "id": "outer-call-1",
        },
        "tool": tool,
        "status": "ok",
    }]
    context = Variant1RunContext.create(
        source="chat", run_id="run-outer",
        metadata={"chat_id": "chat-outer"},
    )
    with bind_run_context(context):
        first = await execute_tool_batch(
            runnable, should_stop=None, ports=_runner_ports(),
            broker=broker_for("first"),
        )
        replay = await execute_tool_batch(
            runnable, should_stop=None, ports=_runner_ports(),
            broker=broker_for("second"),
        )

    assert calls == [{"value": "x"}]
    assert first.outcomes[0]["model_result"] == "applied-once"
    assert replay.outcomes[0]["model_result"] == "applied-once"
    assert replay.outcomes[0]["durable_replay"] is True


@pytest.mark.asyncio
async def test_dispatched_outer_call_is_unknown_after_crash_and_never_reexecutes(
    tmp_path, monkeypatch,
):
    calls = []

    async def write(_args):
        calls.append("called")
        return "effect-applied"

    tool = Tool(
        "ipython", "write", write, params={},
        effect_class="external_side_effect",
    )
    registry = ToolRegistry(); registry.register(tool)
    repository = SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3"))
    repository.ensure_runtime("chat-crash", RuntimeIdentity())
    first_broker = CapabilityBroker(
        registry=registry,
        runtime_registry=_RuntimeRegistry(),
        enabled_resolver=lambda: {"ipython"},
        outer_call_repository=repository,
    )
    monkeypatch.setattr(
        first_broker,
        "finish_provider_outer_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("simulated crash before terminal outer receipt")
        ),
    )
    runnable = [{
        "a": {"tool": "ipython", "args": {}, "id": "outer-crash"},
        "tool": tool,
        "status": "ok",
    }]
    context = Variant1RunContext.create(
        source="chat", run_id="run-crash",
        metadata={"chat_id": "chat-crash"},
    )
    with bind_run_context(context):
        first = await execute_tool_batch(
            runnable, should_stop=None, ports=_runner_ports(),
            broker=first_broker,
        )
        restarted = CapabilityBroker(
            registry=registry,
            runtime_registry=_RuntimeRegistry(),
            enabled_resolver=lambda: {"ipython"},
            outer_call_repository=repository,
        )
        replay = await execute_tool_batch(
            runnable, should_stop=None, ports=_runner_ports(), broker=restarted,
        )

    assert calls == ["called"]
    assert first.outcomes[0]["status"] == "needs_reconciliation"
    assert replay.outcomes[0]["status"] == "needs_reconciliation"
    assert replay.outcomes[0]["durable_replay"] is True


@pytest.mark.asyncio
async def test_required_receipt_sink_failure_changes_effect_to_reconciliation(tmp_path):
    calls = []

    async def write(_args):
        calls.append("applied")
        return "ok"

    tool = Tool(
        "effect", "effect", write, params={}, effect_class="write"
    )
    broker, _registry, _store, _journal = _broker(tmp_path, tool)

    def fail_required(_receipt):
        raise OSError("receipt database full")

    broker.register_receipt_sink(fail_required, required=True)
    receipt = await broker.invoke(
        broker.ref_for_name("effect", catalog_release_id=TEST_RELEASE),
        {}, _context()
    )

    assert calls == ["applied"]
    assert receipt.status == "needs_reconciliation"
    assert receipt.error.code == "receipt_persistence_failed"
    assert receipt.error.may_have_applied is True
    assert receipt.error.retryable is False
