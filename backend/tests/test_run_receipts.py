from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from observability import activity
from agent_engine.runner import _merge_live_run_observability
from chat_pipeline import chat_task
from chat_session import ConnectionSession
from tests.support.conversation_sessions import open_sessions
from llm_router import LLMRouter
from llm_usage import (
    observe_usage,
    observe_usage_category,
)
from run_context import Variant1RunContext, bind_run_context
from observability.run_receipts import (
    build_run_receipt,
    render_previous_run_receipt,
    sanitize_run_receipt,
    wants_previous_run_receipt,
    render_previous_execution_evidence,
)


def test_warm_chat_execution_evidence_does_not_promote_prose_or_tool_bodies():
    import json
    for calls in (0, 2):
        note = render_previous_execution_evidence({
            'run_id': 'run-evidence', 'status': 'completed', 'tool_calls': calls,
            'tool_calls_by_name': {'ipython': calls} if calls else {},
            'assistant': 'I finished everything', 'tool_body': 'private-body',
            'total_tokens': 99999, 'cost_usd': 42,
        })
        evidence = json.loads(note.splitlines()[1])
        assert evidence['prior_run_id'] == 'run-evidence'
        assert 'run_id' not in evidence
        assert evidence['tool_calls'] == calls
        assert evidence['tools'] == ({'ipython': calls} if calls else {})
        assert 'not whether intended task effects succeeded' in note
        assert 'private-body' not in note and 'finished everything' not in note
        assert '99999' not in note and 'cost_usd' not in note
    assert render_previous_execution_evidence({}) == ''
    unknown = render_previous_execution_evidence({'run_id': 'legacy'})
    assert json.loads(unknown.splitlines()[1])['tool_calls'] is None


def test_clean_observed_outcomes_keep_compact_evidence_byte_identical():
    base = {
        'run_id': 'clean-run', 'status': 'ok', 'tool_calls': 2,
        'tool_calls_by_name': {'ipython': 2},
    }
    observed = {
        **base,
        'tool_results_observed': True,
        'tool_result_count': 2,
        'tool_results_by_status': {'ok': 2},
        'tool_error_codes': {},
        'tool_results_without_error_code': 0,
        'tool_result_observations_truncated': 0,
    }
    assert render_previous_execution_evidence(observed) == (
        render_previous_execution_evidence(base)
    )


def test_non_success_outcomes_disambiguate_terminal_status_without_result_prose():
    note = render_previous_execution_evidence({
        'run_id': 'error-run',
        'status': 'ok',
        'tool_calls': 3,
        'tool_calls_by_name': {'ipython': 3},
        'tool_results_observed': True,
        'tool_results_by_status': {'ok': 2, 'error': 1},
        'tool_error_codes': {},
        'tool_results_without_error_code': 1,
        'tool_result_observations_truncated': 0,
        'tool_body': 'ERROR python_exception: private traceback',
    })
    evidence = json.loads(note.splitlines()[1])
    assert evidence == {
        'prior_run_id': 'error-run',
        'run_terminal_status': 'ok',
        'tool_calls': 3,
        'tools': {'ipython': 3},
        'tool_result_count': 3,
        'tool_results_by_status': {'error': 1, 'ok': 2},
        'tool_results_without_error_code': 1,
    }
    assert 'tool_body' not in note and 'private traceback' not in note
    assert 'run settlement' in note


def _event(category: str, tokens: int, seconds: float, cost: float) -> dict:
    return {
        "call_category": category,
        "provider": "xai",
        "model": "grok-test",
        "prompt_tokens": tokens - 2,
        "completion_tokens": 2,
        "total_tokens": tokens,
        "inference_time_s": seconds,
        "cost_usd": cost,
    }


def test_receipt_uses_exact_tool_events_and_categorizes_llm_calls():
    receipt = build_run_receipt(
        run_id="run-1",
        status="ok",
        tool_names=["glob", "grep", "read_file", "read_file", "run_command"],
        usage_events=[
            _event("agent", 10, 1.0, 0.01),
            _event("agent", 20, 2.0, 0.02),
            _event("memory", 3, 0.25, 0.003),
        ],
        wall_time_s=9.5,
    )

    assert receipt["tool_calls"] == 5
    assert receipt["tool_calls_by_name"] == {
        "glob": 1,
        "grep": 1,
        "run_command": 1,
        "read_file": 2,
    }
    assert receipt["tool_sequence"] == [
        "glob", "grep", "read_file", "read_file", "run_command",
    ]
    assert receipt["llm_calls"] == 3
    assert receipt["llm_calls_by_category"] == {
        "agent": 2,
        "memory": 1,
    }
    assert receipt["total_tokens"] == 33
    assert receipt["inference_time_s"] == 3.25
    assert receipt["version"] == 2
    assert receipt["settled"] is False
    assert receipt["direct_usage"]["total_tokens"] == 33


def test_receipt_counts_bounded_tool_result_statuses_and_optional_error_codes():
    observations = [
        {"status": "ok", "error_code": ""},
        {"status": "error", "error_code": "python_exception"},
        {"status": "error", "error_code": ""},
        *({"status": "ok", "error_code": ""} for _ in range(300)),
    ]
    receipt = build_run_receipt(
        run_id="outcomes",
        status="ok",
        tool_names=["ipython"] * 3,
        tool_result_observations=observations,
        usage_events=[],
        wall_time_s=1,
    )
    assert receipt["tool_results_observed"] is True
    assert receipt["tool_result_count"] == 303
    assert receipt["tool_results_by_status"] == {"error": 2, "ok": 254}
    assert receipt["tool_error_codes"] == {"python_exception": 1}
    assert receipt["tool_results_without_error_code"] == 1
    assert receipt["tool_result_observations_truncated"] == 47
    assert receipt["tool_result_status_distribution_truncated"] is True
    assert receipt["tool_error_code_distribution_truncated"] is True
    projected = json.loads(
        render_previous_execution_evidence(receipt).splitlines()[1]
    )
    assert projected["tool_result_count"] == 303
    assert projected["tool_result_observations_truncated"] == 47
    assert projected["tool_result_status_distribution_truncated"] is True


def test_sanitized_legacy_outcomes_remain_unknown_and_current_counters_are_bounded():
    legacy = sanitize_run_receipt({"run_id": "legacy", "status": "ok"})
    assert "tool_results_observed" not in legacy
    assert "tool_results_by_status" not in legacy

    raw_statuses = {f" status {index} ": 99_999 for index in range(100)}
    current = sanitize_run_receipt({
        "run_id": "current",
        "status": "ok",
        "tool_results_observed": True,
        "tool_result_count": 999_999,
        "tool_results_by_status": raw_statuses,
        "tool_error_codes": {" PYTHON_EXCEPTION ": 2},
        "tool_results_without_error_code": 3,
        "tool_result_observations_truncated": 4,
    })
    assert current["tool_results_observed"] is True
    assert len(current["tool_results_by_status"]) == 32
    assert all(count == 10_000 for count in current["tool_results_by_status"].values())
    assert current["tool_error_codes"] == {"python_exception": 2}
    assert current["tool_results_without_error_code"] == 3
    assert current["tool_result_observations_truncated"] == 4
    assert current["tool_result_count"] == 999_999
    assert current["tool_result_status_distribution_truncated"] is True
    assert current["tool_error_code_distribution_truncated"] is True


def test_previous_run_receipt_is_selected_only_for_past_run_audits():
    assert wants_previous_run_receipt(
        "What resulted in 13 tool calls and 8 LLM calls for the task above?"
    )
    assert wants_previous_run_receipt("Why did the previous run take so many tokens?")
    assert wants_previous_run_receipt("What happened in the previous run?")
    assert not wants_previous_run_receipt("Please read the project and summarize it")
    assert not wants_previous_run_receipt("How many tool calls will this new task take?")


def test_rendered_receipt_is_compact_factual_context():
    receipt = build_run_receipt(
        run_id="run-2",
        status="ok",
        tool_names=["glob", "read_file", "read_file"],
        usage_events=[_event("agent", 10, 1.0, 0.01)],
        wall_time_s=2.0,
    )
    text = render_previous_run_receipt(receipt)
    assert text.startswith("## Previous run receipt")
    assert "Tool calls: 3 (glob x1, read_file x2)." in text
    assert "LLM calls: 1 (agent x1)." in text
    assert "Tool sequence: glob -> read_file -> read_file." in text
    assert "use" not in text.lower()


def test_router_stamps_runtime_call_category(tmp_path):
    router = LLMRouter(
        {"mode": "local", "local": {}, "sampling": {}},
        str(tmp_path),
    )
    events = []
    with observe_usage(events.append):
        router._record_usage("local", 3, 2, 5, model="test-agent")
        with observe_usage_category("memory"):
            router._record_usage("local", 4, 1, 5, model="test-memory")
        with observe_usage_category("compaction"):
            router._record_usage("local", 4, 1, 5, model="test-summary")
    assert [event["call_category"] for event in events] == [
        "agent", "memory", "compaction",
    ]


def test_tool_start_events_form_exact_run_sequence():
    seen = []

    class Hub:
        async def broadcast(self, message):
            seen.append(message)

    ctx = Variant1RunContext.create(source="chat", run_id="receipt-run")
    old_hub = activity.HUB
    activity.HUB = Hub()
    try:
        with bind_run_context(ctx):
            asyncio.run(activity.emit_activity("tool:start", tool="glob"))
            asyncio.run(activity.emit_activity("tool:start", tool="read_file"))
            asyncio.run(activity.emit_activity("tool:start", tool="read_file"))
    finally:
        activity.HUB = old_hub
    assert ctx.metadata["_tool_call_names"] == ["glob", "read_file", "read_file"]


def test_tool_result_events_capture_schema_facts_without_bodies_or_inference():
    seen = []

    class Hub:
        async def broadcast(self, message):
            seen.append(message)

    ctx = Variant1RunContext.create(source="chat", run_id="result-receipt-run")
    old_hub = activity.HUB
    activity.HUB = Hub()
    try:
        with bind_run_context(ctx):
            asyncio.run(activity.emit_activity(
                "tool:result", tool="ipython", call_id="call-ok",
                status="ok", text="private successful body",
            ))
            asyncio.run(activity.emit_activity(
                "tool:result", tool="ipython", call_id="call-error",
                status="ERROR", error_code="PYTHON_EXCEPTION",
                text="private traceback body",
            ))
            # A compatibility/UI notification without call identity is not an
            # authoritative provider-tool result and must not enter counters.
            asyncio.run(activity.emit_activity(
                "tool:result", tool="ipython", status="error",
                text="duplicate compatibility body",
            ))
    finally:
        activity.HUB = old_hub

    assert ctx.metadata["_tool_result_observations"] == [
        {"status": "ok", "error_code": ""},
        {"status": "error", "error_code": "python_exception"},
    ]
    assert "private" not in json.dumps(ctx.metadata)
    assert len(seen) == 3


def test_native_inner_run_observability_returns_to_chat_receipt_context():
    parent = Variant1RunContext.create(source="chat", run_id="same-run")
    child = Variant1RunContext.create(source="chat", run_id="same-run")
    parent.metadata["_tool_call_names"] = ["existing"]
    parent.metadata["tools_used"] = ["existing"]
    child.metadata["_tool_call_names"] = ["ipython", "ipython"]
    child.metadata["tools_used"] = ["ipython"]
    child.metadata["_terminal_status"] = "ok"
    parent.metadata["_tool_result_observations"] = [
        {"status": "ok", "error_code": ""},
    ]
    child.metadata["_tool_result_observations"] = [
        {"status": "error", "error_code": "python_exception"},
    ]
    child.metadata["_tool_result_observations_truncated"] = 2

    _merge_live_run_observability(parent, child)

    assert parent.metadata["_tool_call_names"] == [
        "existing", "ipython", "ipython",
    ]
    assert parent.metadata["tools_used"] == ["existing", "ipython"]
    assert parent.metadata["_terminal_status"] == "ok"
    assert parent.metadata["_tool_result_observations"] == [
        {"status": "ok", "error_code": ""},
        {"status": "error", "error_code": "python_exception"},
    ]
    assert parent.metadata["_tool_result_observations_truncated"] == 2


def test_inner_run_outcome_merge_is_bounded_and_accounts_for_every_omission():
    parent = Variant1RunContext.create(source="chat", run_id="bounded-run")
    child = Variant1RunContext.create(source="chat", run_id="bounded-run")
    parent.metadata["_tool_result_observations"] = [
        {"status": "ok", "error_code": ""} for _ in range(250)
    ]
    child.metadata["_tool_result_observations"] = [
        {"status": "error", "error_code": ""} for _ in range(300)
    ]
    child.metadata["_tool_result_observations_truncated"] = 5

    _merge_live_run_observability(parent, child)

    assert len(parent.metadata["_tool_result_observations"]) == 256
    # 44 child rows are bounded before merge; another 250 are omitted when the
    # retained child prefix is appended to the parent's 250 rows; five were
    # already omitted by the child recorder.
    assert parent.metadata["_tool_result_observations_truncated"] == 299


async def _noop_send(_message):
    return None


def _make_chat_context(_kind, _text, *, session, chat_transport, metadata):
    return Variant1RunContext.create(
        source="chat",
        run_id="chat-receipt-run",
        chat_session=session,
        chat_transport=chat_transport,
        metadata=metadata,
    )


def test_chat_task_receipt_tracks_usage_and_terminal_status(tmp_path):
    async def scenario():
        store = open_sessions(tmp_path / "sessions")
        sid = store.create_session()
        router = LLMRouter(
            {"mode": "local", "local": {}, "sampling": {}},
            str(tmp_path / "router"),
        )
        async def handle_chat(*_args, **_kwargs):
            await activity.emit_activity("tool:start", tool="glob")
            await activity.emit_activity("tool:start", tool="read_file")
            await activity.emit_activity(
                "tool:result", tool="glob", call_id="call-glob", status="ok",
            )
            await activity.emit_activity(
                "tool:result", tool="read_file", call_id="call-read",
                status="error",
            )
            router._record_usage("local", 8, 2, 10, model="main")
            await activity.emit_activity("task:done", status="failed")

            with observe_usage_category("memory"):
                router._record_usage("local", 3, 1, 4, model="memory")

        emit = AsyncMock()
        ports = SimpleNamespace(
            io=SimpleNamespace(sessions=store, emit=emit),
            session=SimpleNamespace(
                make_run_context=_make_chat_context,
                handle_chat=handle_chat,
            ),
        )
        messages = []

        async def capture(message):
            messages.append(message)

        websocket = SimpleNamespace(send_json=capture)
        session = ConnectionSession()
        seq = session.reserve_turn()
        session.active.turn_session_id = sid

        await chat_task(ports, websocket, "audit me", session, turn_seq=seq)
        receipt = store.get_last_run_receipt(sid)
        assert receipt["tool_sequence"] == ["glob", "read_file"]
        assert receipt["llm_calls_by_category"] == {"agent": 1, "memory": 1}
        assert receipt["llm_calls"] == 2
        assert receipt["total_tokens"] == 14
        assert receipt["status"] == "failed"
        assert receipt["version"] == 2
        assert receipt["settled"] is True
        assert receipt["tool_results_by_status"] == {"error": 1, "ok": 1}
        assert receipt["tool_error_codes"] == {}
        assert receipt["tool_results_without_error_code"] == 1
        settled = [row for row in messages if row.get("type") == "run:settled"]
        assert len(settled) == 1
        assert settled[0]["receipt"] == receipt
        resource_calls = [
            call for call in emit.await_args_list
            if call.args and call.args[0] == "task:resources"
        ]
        assert len(resource_calls) == 1
        assert resource_calls[0].kwargs["status"] == "failed"
        assert resource_calls[0].kwargs["receipt"]["status"] == "failed"

    asyncio.run(scenario())
