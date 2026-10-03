from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

from capability_broker import CapabilityBroker
from kernel_runtime.contracts import KernelExecutionResult, MAX_MODEL_ERROR_CHARS
from kernel_runtime.output import CellOutput, CellOutputCollector, OutputLimits
from kernel_runtime.repl_protocol import encode_line, request_frame
import kernel_runtime.repl_worker as worker_module
from kernel_runtime.repl_worker import ReplWorker, _CellStreamBudget, _EventTextStream
from tools import ToolRegistry


def test_utf8_cap_accounts_for_complete_original_points():
    collector = CellOutputCollector(limits=OutputLimits(max_message_bytes=4, max_cell_bytes=4))
    collector._admit("stdout", "€😀")
    assert collector.result.chunks[0]["text"] == "€"
    assert collector.result.admitted_bytes == 3
    assert collector.result.dropped_bytes == 4
    collector._admit("stderr", "€")
    assert collector.result.admitted_bytes == 3
    assert collector.result.dropped_bytes == 7


def test_no_store_result_is_marked_and_byte_bounded():
    broker = CapabilityBroker(registry=ToolRegistry(), runtime_registry=SimpleNamespace(), enabled_resolver=set, inline_result_bytes=1024)
    blocks, refs, truncation = broker._project_result("€" * 1000, receipt_id="r", scope="s")
    assert not refs
    assert "truncated" in blocks[0].text and "�" not in blocks[0].text
    assert len(blocks[0].text.encode("utf-8")) <= broker.inline_result_bytes
    assert truncation.admitted_bytes + truncation.dropped_bytes == 3000
    binary, _, dropped = broker._project_result(b"\xff" * 100, receipt_id="r", scope="s")
    assert "Binary result unavailable" in binary[0].text
    assert dropped.admitted_bytes == 0 and dropped.dropped_bytes == 100


def test_long_exception_keeps_labels_heading_and_omission():
    output = CellOutput(error_name="RuntimeError", error_value="failure" * 2000, traceback=["trace" * 5000])
    output.chunks.append({"kind": "stderr", "text": "last output" * 1000})
    result = KernelExecutionResult("exec", "chat", 1, "error", output, error_code="python_exception")
    text = result.render()
    assert len(text) <= MAX_MODEL_ERROR_CHARS
    assert text.startswith("ERROR python_exception: RuntimeError")
    assert all(label in text for label in ("[diagnostic truncated]", "Output tail:", "Traceback tail:"))


def test_stream_chunking_keeps_unicode_and_request_ownership():
    events, active = [], ["first"]
    emitter = SimpleNamespace(emit=lambda kind, request_id, **fields: events.append((kind, request_id, fields["text"])))
    context = SimpleNamespace(current_request_id=lambda: active[0])
    stream = _EventTextStream(context, emitter, "stdout", chunk_bytes=2, budget=_CellStreamBudget(100_000))
    text = "α€😀\n" * 100
    stream.write(text)
    active[0] = "second"
    stream.write("tail")
    stream.flush()
    assert "".join(row[2] for row in events if row[1] == "first") == text
    assert "".join(row[2] for row in events if row[1] == "second") == "tail"
    assert not stream._pending


def test_non_python_error_marker_fits_the_diagnostic_budget():
    result = KernelExecutionResult("exec", "chat", 1, "error", CellOutput(),
                                   error_code="kernel_protocol_error", error_message="x" * 20_000)
    text = result.render()
    assert len(text) <= MAX_MODEL_ERROR_CHARS
    assert text.startswith("ERROR kernel_protocol_error:") and text.endswith("[diagnostic truncated]")


def _read_frames(monkeypatch, chunks, limit):
    frames = queue.SimpleQueue()
    reads = iter([*chunks, b""])
    worker = ReplWorker.__new__(ReplWorker)
    worker.loop = SimpleNamespace(call_soon_threadsafe=lambda callback, item: callback(item))
    worker.queue = frames
    worker.control = SimpleNamespace(fileno=lambda: 0)
    worker.control_read_allowed = threading.Event()
    worker.control_read_allowed.set()
    worker.emitter = SimpleNamespace(max_frame_bytes=limit)
    worker._interrupt_lock = threading.RLock()
    worker._queued_execution_ids = set()
    worker._reader_interrupt = lambda frame: frames.put({"interrupt": frame["id"]})
    monkeypatch.setattr(worker_module.os, "read", lambda *_: next(reads))
    worker._read_requests()
    result = []
    while not frames.empty():
        result.append(frames.get())
    return result


def test_large_fragmented_frame_searches_new_bytes_and_preserves_source(monkeypatch):
    searched = [0]
    class Counted(bytearray):
        def find(self, value, start=0, *args):
            searched[0] += len(self) - start
            return super().find(value, start, *args)
    monkeypatch.setattr(worker_module, "bytearray", Counted, raising=False)
    code = "x" * (2 * 1024 * 1024)
    raw = encode_line(request_frame("big", "execute", code=code), max_bytes=4 * 1024 * 1024)
    frames = _read_frames(monkeypatch, [raw[i:i + 65536] for i in range(0, len(raw), 65536)], 4 * 1024 * 1024)
    assert frames[0]["code"] == code
    assert searched[0] < 3 * len(raw)


def test_oversize_resynchronizes_once_before_interrupt_and_next_frame(monkeypatch):
    interrupt = encode_line(request_frame("stop", "interrupt", target_id="next"))
    valid = encode_line(request_frame("next", "execute", code="pass"))
    frames = _read_frames(monkeypatch, [b"x" * 300, b"x" * 300, b"\n" + interrupt + valid], 256)
    errors = [f for f in frames if isinstance(f, dict) and "_protocol_error" in f]
    assert errors == [{"_protocol_error": "REPL frame is oversized"}]
    assert {"interrupt": "stop"} in frames
    assert any(isinstance(f, dict) and f.get("id") == "next" for f in frames)
