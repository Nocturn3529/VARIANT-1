from __future__ import annotations

import asyncio
import json
import threading
import time

from observability import activity
from agent_types import ToolBatchResult
from agent_engine.state import OBSERVABILITY_TAIL_LIMIT, new_run_state, queue_event
from run_context import Variant1RunContext, bind_run_context
from observability.trace_events import (
    RECORDER,
    TRACE_SCHEMA,
    TraceRecorder,
    read_trace_events,
    record_model_manifest,
)
from llm_manifest_bus import ModelRequestManifestBus
from tool_runner import ToolRunnerPorts, execute_tool_batch


def _rows(path):
    assert RECORDER.flush()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_queue_event_keeps_bounded_checkpoint_tail_and_full_external_trace(tmp_path):
    path = tmp_path / "events.jsonl"
    RECORDER.configure_for_tests(path=str(path), enabled=True)
    try:
        state = new_run_state(source="chat", title="t", goal="g", run_id="run_trace")
        for index in range(OBSERVABILITY_TAIL_LIMIT + 9):
            state["observability"] = queue_event(state, "agent_runtime:step", step=index)

        assert len(state["observability"]) == OBSERVABILITY_TAIL_LIMIT
        assert state["observability"][0]["fields"]["step"] == 9
        rows = _rows(path)
        assert len(rows) == OBSERVABILITY_TAIL_LIMIT + 9
        assert rows[0]["schema"] == TRACE_SCHEMA
        assert rows[-1]["attributes"]["step"] == OBSERVABILITY_TAIL_LIMIT + 8
        assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
    finally:
        RECORDER.reset_configuration()


def test_activity_trace_uses_bound_run_identity_and_call_span(tmp_path):
    path = tmp_path / "events.jsonl"
    RECORDER.configure_for_tests(path=str(path), enabled=True)
    old_hub = activity.HUB

    class Hub:
        async def broadcast(self, _message):
            return None

    activity.HUB = Hub()
    try:
        ctx = Variant1RunContext.create(
            run_id="run_activity",
            thread_id="thread_activity",
            session_id="session_activity",
            source="chat",
        )
        with bind_run_context(ctx):
            asyncio.run(activity.emit_activity(
                "tool:result",
                tool="read_file",
                call_id="call_7",
                status="ok",
            ))
        row = _rows(path)[0]
        assert row["run_id"] == "run_activity"
        assert row["thread_id"] == "thread_activity"
        assert row["session_id"] == "session_activity"
        assert row["kind"] == "tool"
        assert row["phase"] == "end"
        assert row["attributes"]["call_id"] == "call_7"
        assert len(row["trace_id"]) == 32
        assert len(row["span_id"]) == 16
    finally:
        activity.HUB = old_hub
        RECORDER.reset_configuration()


def test_model_manifest_trace_retains_metrics_not_prompt_values(tmp_path):
    path = tmp_path / "events.jsonl"
    RECORDER.configure_for_tests(path=str(path), enabled=True)
    try:
        record_model_manifest({
            "manifest_id": "mreq_1",
            "logical_call_id": "logical_1",
            "run": {
                "run_id": "run_model",
                "thread_id": "thread_model",
                "session_id": "session_model",
            },
            "route": {"provider": "local", "model": "test-model", "api_style": "openai"},
            "generation": {"max_output_tokens": 128},
            "tools": {"rendered_count": 1, "rendered_schema_sha256": "abc"},
            "budget": {"estimated_input_tokens_lower_bound": 42, "over_budget": False},
            "privacy": {"prompt_text_stored": False},
            "messages": {"raw_secret": "must not be copied"},
        })
        row = _rows(path)[0]
        assert row["event"] == "model:request_manifest"
        assert row["phase"] == "start"
        assert row["attributes"]["manifest_id"] == "mreq_1"
        assert row["attributes"]["estimated_input_tokens"] == 42
        assert row["session_id"] == "session_model"
        assert "raw_secret" not in json.dumps(row)
    finally:
        RECORDER.reset_configuration()


def test_exporter_is_fail_open_when_it_raises(tmp_path):
    path = tmp_path / "events.jsonl"
    RECORDER.configure_for_tests(path=str(path), enabled=True)
    RECORDER.install_exporter(lambda _event: (_ for _ in ()).throw(RuntimeError("nope")))
    try:
        queue_event({}, "agent_runtime:init", source="chat")
        assert len(_rows(path)) == 1
    finally:
        RECORDER.reset_configuration()


def test_usage_patch_emits_correlated_usage_event(tmp_path):
    path = tmp_path / "events.jsonl"
    RECORDER.configure_for_tests(path=str(path), enabled=True)
    try:
        bus = ModelRequestManifestBus()
        asyncio.run(bus.record({
            "manifest_id": "mreq_usage",
            "logical_call_id": "logical_usage",
            "run": {
                "run_id": "run_usage",
                "thread_id": "thread_usage",
                "session_id": "session_usage",
            },
            "route": {"provider": "local", "model": "m"},
        }))
        bus.patch_usage("mreq_usage", {
            "measurement": "provider_reported",
            "provider_reported": True,
            "estimated": False,
            "call_category": "compaction",
            "input_tokens": 10,
            "output_tokens": 4,
            "total_tokens": 14,
            "cached_input_tokens": 3,
            "reasoning_tokens": 2,
        })
        row = _rows(path)[0]
        assert row["event"] == "model:usage"
        assert row["phase"] == "end"
        assert row["attributes"]["manifest_id"] == "mreq_usage"
        assert row["attributes"]["total_tokens"] == 14
        assert row["attributes"]["prompt_tokens"] == 10
        assert row["attributes"]["completion_tokens"] == 4
        assert row["attributes"]["cached_tokens"] == 3
        assert row["attributes"]["reasoning_tokens"] == 2
        assert row['attributes']['measurement'] == 'provider_reported'
        assert row['attributes']['provider_reported'] is True
        assert row['attributes']['estimated'] is False
        assert row['attributes']['call_category'] == 'compaction'
        assert row["run_id"] == "run_usage"
        assert row["session_id"] == "session_usage"
    finally:
        RECORDER.reset_configuration()


def test_tool_trace_pairs_start_and_result_by_call_id_without_arguments(tmp_path):
    path = tmp_path / "events.jsonl"
    RECORDER.configure_for_tests(path=str(path), enabled=True)

    class Tool:
        async def run(self, args):
            assert args == {"path": "C:/private/value.txt"}
            await asyncio.sleep(0.005)
            return "ok"

    async def emit(event, **fields):
        await activity.emit_activity(event, **fields)

    async def send_running(_name, _args, _call_id):
        return None

    ports = ToolRunnerPorts(
        emit=emit,
        send_running=send_running,
        clip=lambda value, limit: str(value)[:limit],
        max_result_chars=1_000,
    )
    old_hub = activity.HUB

    class Hub:
        async def broadcast(self, _message):
            return None

    activity.HUB = Hub()
    try:
        ctx = Variant1RunContext.create(
            run_id="run_tool",
            thread_id="thread_tool",
            source="chat",
        )
        with bind_run_context(ctx):
            runnable = [{
                "a": {
                    "id": "call_private",
                    "tool": "read_file",
                    "args": {"path": "C:/private/value.txt"},
                },
                "tool": Tool(),
                "status": "ok",
            }]
            from tests.test_tool_runner_concurrency import _RunnerBroker

            result = asyncio.run(execute_tool_batch(
                runnable,
                should_stop=lambda: False,
                ports=ports,
                broker=_RunnerBroker(runnable),
            ))
        assert isinstance(result, ToolBatchResult)
        rows = _rows(path)
        admissions = [row for row in rows if row["event"] == "tool:admission"]
        starts = [row for row in rows if row["event"] == "tool:start"]
        results = [row for row in rows if row["event"] == "tool:result"]
        assert len(admissions) == len(starts) == len(results) == 1
        assert starts[0]["span_id"] == results[0]["span_id"]
        assert starts[0]["attributes"]["call_id"] == "call_private"
        assert len(starts[0]["attributes"]["args_sha256"]) == 64
        assert "C:/private/value.txt" not in json.dumps(rows)
        assert "review_before_ms" not in admissions[0]["attributes"]
        assert "review_before_ms" not in starts[0]["attributes"]
        assert results[0]["attributes"]["duration_ms"] >= 0
        assert "review_after_ms" not in results[0]["attributes"]
        assert results[0]["attributes"]["total_duration_ms"] >= results[0]["attributes"]["duration_ms"]
    finally:
        activity.HUB = old_hub
        RECORDER.reset_configuration()


def test_trace_query_filters_and_caps_without_replay(tmp_path):
    path = tmp_path / "events.jsonl"
    RECORDER.configure_for_tests(path=str(path), enabled=True)
    try:
        for index in range(6):
            RECORDER.record(
                "tool:result" if index % 2 else "agent_runtime:step",
                run_id="run_a" if index < 5 else "run_b",
                thread_id="thread_1",
                session_id="session_a" if index < 4 else "session_b",
                index=index,
            )
        rows = read_trace_events(path=str(path), run_id="run_a", limit=2)
        assert [row["attributes"]["index"] for row in rows] == [3, 4]
        tool_rows = read_trace_events(
            path=str(path),
            thread_id="thread_1",
            event="tool:result",
        )
        assert [row["attributes"]["index"] for row in tool_rows] == [1, 3, 5]
        session_rows = read_trace_events(
            path=str(path), session_id="session_a",
        )
        assert [row["attributes"]["index"] for row in session_rows] == [0, 1, 2, 3]
    finally:
        RECORDER.reset_configuration()


def test_slow_exporter_cannot_delay_canonical_jsonl(tmp_path):
    path = tmp_path / "events.jsonl"
    recorder = TraceRecorder()
    recorder.configure_for_tests(path=str(path), enabled=True)
    entered = threading.Event()
    release = threading.Event()

    def slow_exporter(_event):
        entered.set()
        release.wait(2)

    recorder.install_exporter(slow_exporter)
    try:
        recorder.record("tool:result", run_id="run_slow", status="ok")
        assert recorder.flush(timeout=0.5)
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "run_slow"
        assert entered.wait(0.5)
    finally:
        release.set()
        assert recorder.flush_exporters(timeout=1)
        recorder.reset_configuration()


def test_disk_writer_does_not_hold_the_producer_sequence_lock(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    recorder = TraceRecorder()
    recorder.configure_for_tests(path=str(path), enabled=True)
    entered = threading.Event()
    release = threading.Event()
    original_rotate = recorder._rotate_if_needed

    def slow_rotate(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original_rotate(*args, **kwargs)

    monkeypatch.setattr(recorder, "_rotate_if_needed", slow_rotate)
    try:
        recorder.record("tool:start", run_id="run_lock")
        assert entered.wait(0.5)
        started = time.monotonic()
        envelope = recorder.envelope("tool:result", {"run_id": "run_lock"})
        assert time.monotonic() - started < 0.1
        assert envelope["sequence"] == 2
    finally:
        release.set()
        assert recorder.flush(timeout=1)
        recorder.reset_configuration()


def test_flush_restarts_a_dead_writer(tmp_path):
    path = tmp_path / "events.jsonl"
    recorder = TraceRecorder()
    recorder.configure_for_tests(path=str(path), enabled=True)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    recorder._worker = dead

    recorder.record("run:settled", run_id="run_restart", status="ok")

    assert recorder.durability_barrier(timeout=1)
    assert recorder.health()["writer_alive"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "run_restart"
    recorder.reset_configuration()
