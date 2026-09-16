from __future__ import annotations

from dataclasses import dataclass

from observability.operational_log import (
    emit,
    mirror_activity,
    mirror_model_manifest,
    mirror_model_usage,
    mirror_snapshot,
    mirror_trace,
    mirror_work_terminal,
)


def _lines(capsys) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if line]


def test_emit_is_parseable_bounded_and_omits_payload_fields(capsys):
    emit(
        "model",
        "request",
        provider="test",
        prompt="private prompt",
        headers={"Authorization": "Bearer hidden"},
        note="Bearer abcdefghijklmnopqrstuvwxyz",
    )
    line = _lines(capsys)[0]
    assert line.startswith("[op] level=info source=model event=request")
    assert 'provider="test"' in line
    assert "private prompt" not in line
    assert "Authorization" not in line
    assert "abcdefghijklmnopqrstuvwxyz" not in line
    assert "<redacted>" in line


def test_activity_projection_keeps_lifecycle_not_perception_telemetry(capsys):
    mirror_activity({"event": "perception:quality_metrics", "status": "ok"})
    assert _lines(capsys) == []

    mirror_activity({
        "event": "tool:result",
        "status": "failed",
        "tool": "computer.click",
        "call_id": "call_1",
        "output": "private result",
    })
    line = _lines(capsys)[0]
    assert "source=tool" in line
    assert "level=error" in line
    assert 'tool="computer.click"' in line
    assert "private result" not in line


def test_trace_and_snapshot_projection_select_only_operational_events(capsys):
    mirror_trace("agent_runtime:step", {"status": "ok"})
    assert _lines(capsys) == []

    mirror_trace("broker:result", {
        "status": "failed",
        "capability_name": "connector.invoke",
        "error_code": "schema_error",
        "payload": {"secret": "no"},
    })
    line = _lines(capsys)[0]
    assert "source=capability" in line
    assert 'error_code="schema_error"' in line
    assert "secret" not in line

    mirror_snapshot("resume_scan", {"result": "no_snapshot"})
    assert _lines(capsys) == []
    mirror_snapshot("lookup_hit", {"result": "restored", "thread_id": "chat_1"})
    assert "source=snapshot" in _lines(capsys)[0]


def test_model_projection_has_route_and_usage_but_no_messages(capsys):
    manifest = {
        "manifest_id": "req_1",
        "logical_call_id": "logical_1",
        "run": {"run_id": "run_1", "session_id": "chat_1"},
        "route": {"provider": "local", "model": "unit", "transport": "sse"},
        "generation": {"max_output_tokens": 4096},
        "tools": {"rendered_count": 1},
        "messages": [{"content": "private"}],
    }
    mirror_model_manifest(manifest)
    start = _lines(capsys)[0]
    assert "event=request_start" in start
    assert 'model="unit"' in start
    assert 'run_id="run_1"' in start
    assert 'session_id="chat_1"' in start
    assert "private" not in start

    mirror_model_usage({
        **manifest,
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "cached_tokens": 2},
    })
    done = _lines(capsys)[0]
    assert "event=request_complete" in done
    assert "input_tokens=10" in done
    assert "output_tokens=4" in done
    assert 'run_id="run_1"' in done
    assert 'session_id="chat_1"' in done


@dataclass
class _Event:
    event_type: str = "job.failed"
    aggregate_id: str = "job_1"


@dataclass
class _Job:
    kind: str = "process"
    owner_kind: str = "chat"
    owner_id: str = "chat_1"
    attempt: int = 2


def test_terminal_work_projection(capsys):
    mirror_work_terminal(_Event(), _Job())
    line = _lines(capsys)[0]
    assert "source=work" in line
    assert "event=job.failed" in line
    assert "level=error" in line
    assert "attempt=2" in line
