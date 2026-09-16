"""Failure-boundary regressions for the two Grok bug-hunt checklists."""
import asyncio
import gc
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tool_calling import ToolCallAccumulator
from tests.test_capability_broker import _broker, _context, _echo_tool, TEST_RELEASE


@pytest.mark.asyncio
async def test_unavailable_capability_is_finalized_and_deduplicated(tmp_path):
    broker, _, _, receipts = _broker(tmp_path)
    first = await broker.invoke_name("missing", {}, _context())
    second = await broker.invoke_name("missing", {}, _context())
    assert first.error.code == "capability_unavailable"
    assert first.effect.observed_at and receipts == [first]
    assert second.deduplicated and second.receipt_id == first.receipt_id


@pytest.mark.asyncio
async def test_metadata_failure_never_leaves_a_receiptless_admission(tmp_path, monkeypatch):
    calls = []
    tool = _echo_tool(calls)
    broker, _, _, receipts = _broker(tmp_path, tool)
    ref = broker.ref_for_name("echo", catalog_release_id=TEST_RELEASE)
    metadata = broker.metadata_for_tool
    def broken(tool, args=None):
        if args is not None:
            raise RuntimeError("metadata projection failed")
        return metadata(tool)
    monkeypatch.setattr(broker, "metadata_for_tool", broken)
    first = await broker.invoke(ref, {"value": "x"}, _context())
    replay = await broker.invoke(ref, {"value": "x"}, _context())
    assert first.error.code == "capability_admission_interrupted"
    assert not first.error.may_have_applied and not first.effect.attempted_at
    assert receipts == [first] and replay.deduplicated and calls == []


def test_structured_arguments_and_text_deltas_never_concatenate_two_objects():
    acc = ToolCallAccumulator()
    def add(args):
        acc.add_openai_delta([{"index": 0, "id": "call", "function": {"name": "echo", "arguments": args}}])
    add({})
    add('{"value":')
    add('"streamed"}')
    assert acc.actions()[0]["args"] == {"value": "streamed"}
    add({"value": "final"})
    add('{"value":"final"}')
    assert acc.actions()[0]["args"] == {"value": "final"}
    assert "argument_error" not in acc.actions()[0]


def test_conflicting_mixed_argument_encodings_are_not_executable():
    acc = ToolCallAccumulator()
    for args in ({"value": "one"}, '{"value":"other"}'):
        acc.add_openai_delta([{"function": {"name": "echo", "arguments": args}}])
    assert acc.actions()[0]["argument_error"] and acc.actions()[0]["args"] == {}


def test_complete_decoded_arguments_survive_redundant_partial_text():
    acc = ToolCallAccumulator()
    for args in ({"value": "one"}, 'one"}'):
        acc.add_openai_delta([{"function": {"name": "echo", "arguments": args}}])
    assert acc.actions()[0]["args"] == {"value": "one"}
    assert "argument_error" not in acc.actions()[0]


def test_gemini_without_provider_ids_preserves_distinct_calls():
    acc = ToolCallAccumulator()
    for value in ("a", "b"):
        acc.add_gemini_function_call({"name": "echo", "args": {"value": value}})
    assert [action["id"] for action in acc.actions()] == ["gemini_0", "gemini_1"]


@pytest.mark.asyncio
async def test_finalizer_failure_releases_an_internally_reserved_chat(tmp_path, monkeypatch):
    import chat_pipeline
    from chat_session import ConnectionSession
    from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository
    from tests.support.conversation_sessions import open_sessions
    from tests.test_persist_interrupted_turn import _chat_context
    sessions = open_sessions(tmp_path / "chats")
    sid = sessions.create_session()
    registry = SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3")))
    session = ConnectionSession(viewed_session_id=sid)
    seq = session.reserve_turn()
    ports = SimpleNamespace(io=SimpleNamespace(sessions=sessions, emit=AsyncMock(), runtime_registry=registry,
            hub=SimpleNamespace(broadcast=AsyncMock())),
        session=SimpleNamespace(make_run_context=_chat_context, handle_chat=AsyncMock()))
    monkeypatch.setattr(chat_pipeline, "settle_undelivered_inputs", AsyncMock(side_effect=RuntimeError("settle failed")))
    with pytest.raises(RuntimeError, match="settle failed"):
        await chat_pipeline.chat_task(ports, SimpleNamespace(send_json=AsyncMock()), "hello", session, turn_seq=seq)
    assert not registry.is_busy(sid) and not session.busy
    next_admission = await registry.reserve_run(sid, attachment_id=session.attachment_id)
    assert next_admission
    registry.finish_run(next_admission, status="test_complete")


@pytest.mark.asyncio
async def test_pending_cancel_capacity_does_not_forget_an_acknowledged_cancel():
    from kernel_runtime.bridge import KernelBridgeServer
    observed = []
    async def invoke(request):
        observed.append(request["_bridge_cancel_event"].is_set())
        return {"ok": True}
    bridge = KernelBridgeServer(secret=b"fixture", nonce="n", generation=1, invoke_handler=invoke)
    bridge.cancellation_capacity = 2
    bridge._handshaken = True
    def cancel(target, origin="a"):
        return {"request_id": "cancel-" + target, "target_request_id": target,
                "execution_id": origin, "outer_tool_call_id": "outer"}
    assert (await bridge._cancel_request(cancel("first")))["ok"]
    assert (await bridge._cancel_request(cancel("second")))["ok"]
    assert not (await bridge._cancel_request(cancel("third")))["ok"]
    await bridge._dispatch({"op": "invoke", "generation": 1, "request_id": "first", "execution_id": "a", "outer_tool_call_id": "outer"})
    assert observed == [True]
    await bridge.forget_execution("a")
    assert not bridge._pending_cancellations


@pytest.mark.asyncio
async def test_cancelled_uia_does_not_hold_input_lock_but_quarantines_pending_com(monkeypatch):
    from desktop import uia_worker
    from tools import ToolError
    release = threading.Event()
    entered = threading.Event()
    flushed = []
    pool = uia_worker._DaemonSingleThreadExecutor(name="bug-hunt-uia")
    monkeypatch.setattr(uia_worker, "_UIA_EXEC", pool)
    monkeypatch.setattr(uia_worker, "_COM_STATUS", "")
    monkeypatch.setattr(uia_worker, "com_begin", lambda: None)
    monkeypatch.setattr(uia_worker, "_ABANDONED_WORK", [])
    def com():
        entered.set()
        release.wait(5)
    async def flush():
        flushed.append(True)
    task = asyncio.create_task(uia_worker.run(com, timeout_s=5, after_batch=flush))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        with pytest.raises(ToolError, match="blocked"):
            await uia_worker.run(lambda: pytest.fail("concurrent input"))
        assert not flushed
        release.set()
        for _ in range(100):
            if not uia_worker._hung_work_pending():
                break
            await asyncio.sleep(.01)
        assert flushed == [True] and not uia_worker._hung_work_pending()
    finally:
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_idle_semantic_locks_do_not_accumulate(tmp_path):
    from tests.test_desktop_fabric import runtime
    fabric, _ = runtime(tmp_path)
    first = await fabric._semantic_lock("w")
    assert first is await fabric._semantic_lock("w")
    del first
    gc.collect()
    assert not fabric._semantic_locks


@pytest.mark.parametrize("url", ["wss://attacker.test", "ws://gateway.discord.gg", "wss://gateway.discord.gg.attacker.test", "wss://u:p@gateway.discord.gg"])
def test_discord_gateway_rejects_foreign_or_unencrypted_destinations(url):
    from messaging_gateway.adapters.discord import gateway_url
    with pytest.raises(ValueError):
        gateway_url(url)
    assert gateway_url("wss://gateway-us-east1.discord.gg/") == "wss://gateway-us-east1.discord.gg"


@pytest.mark.asyncio
async def test_discord_attachment_rejects_other_hosts_before_http(tmp_path):
    from messaging_gateway.media import download_and_stage
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected fetch"))) as client:
        with pytest.raises(ValueError, match="HTTPS CDN"):
            await download_and_stage(client, "http://127.0.0.1/private", str(tmp_path), adapter="discord",
                                     conversation_id="c", message_id="m", name="file.txt")


def test_llama_selection_rejects_multiple_binaries_and_partial_version_match(tmp_path, monkeypatch):
    from model_runtime import llama_runtime
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir(); second.mkdir()
    for directory in (first, second):
        (directory / "llama-server.exe").write_bytes(b"fixture")
    with pytest.raises(llama_runtime.LlamaRuntimeError, match="multiple"):
        llama_runtime.server_binary(tmp_path)
    monkeypatch.setattr(llama_runtime.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout="version: 110679"))
    with pytest.raises(llama_runtime.LlamaRuntimeError, match="mismatch"):
        llama_runtime.verify_install(first, "b10679")


@pytest.mark.asyncio
async def test_llama_binary_activation_restarts_live_process_and_rolls_back_failure(tmp_path):
    from model_runtime.engine_manager import activate_llama_binary
    from tests.test_engine_manager_lifecycle import _Engine, _Router
    engine = _Engine()
    engine.runtime_id, engine.binary, engine.backend = "llamacpp", "old.exe", "cpu"
    router = _Router(engine)
    router.cfg["local"].update(binary="old.exe", backend="cpu")
    launches = []
    async def restart(model, projector):
        launches.append(engine.binary)
        if engine.binary == "bad.exe":
            raise RuntimeError("failed startup")
        engine.ready = True
    engine.restart = restart
    await activate_llama_binary(router, binary="new.exe", backend="cuda", tag="b2")
    assert launches == ["new.exe"] and router.cfg["local"]["binary"] == "new.exe"
    with pytest.raises(RuntimeError, match="failed startup"):
        await activate_llama_binary(router, binary="bad.exe", backend="cuda", tag="b3")
    assert launches[-2:] == ["bad.exe", "new.exe"] and engine.binary == "new.exe"
    assert router.cfg["local"]["binary"] == "new.exe"


def test_custom_llama_is_not_called_packaged(tmp_path):
    from model_runtime import llama_runtime
    binary = tmp_path / "custom.exe"
    binary.write_bytes(b"fixture")
    result = llama_runtime.status(str(tmp_path), configured_binary=str(binary), bundled_binary=str(tmp_path / "app/bin/llama-server.exe"))
    assert result["install_source"] == "custom" and not result["bundled_active"]


@pytest.mark.asyncio
async def test_multi_archive_progress_is_monotonic_through_download_and_extract(tmp_path, monkeypatch):
    from model_runtime.runtime_installer import RuntimeInstaller
    values = []
    job = {"id": "test", "progress": 0, "backend": "cuda"}
    installer = RuntimeInstaller(str(tmp_path), AsyncMock())
    def install(*args, progress, **kwargs):
        for index in (1, 2):
            for stage, done in (("download", 0), ("download", 100), ("verify", 0), ("extract", 0), ("extract", 100)):
                progress(stage, done, 100, f"{index}/2")
                values.append(job["progress"])
        progress("verify", 0, 0, "")
        values.append(job["progress"])
        return {"binary": "fixture.exe", "backend": "cuda", "tag": "b1"}
    monkeypatch.setattr("model_runtime.llama_runtime.install_runtime", install)
    await installer._run_llama_job(job)
    assert values == sorted(values)
    assert values[4] < values[-1] and values[-1] == 96


@pytest.mark.asyncio
async def test_missing_memory_owner_is_rejected_before_paid_extraction(monkeypatch):
    import host_memory_ops
    extract = AsyncMock()
    monkeypatch.setattr(host_memory_ops.memory_tools, "extract_and_store", extract)
    monkeypatch.setattr(host_memory_ops, "_chat_id", lambda: "")
    with pytest.raises(ValueError, match="session_id"):
        await host_memory_ops.extract_and_store(SimpleNamespace(), "remember", "reply")
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_memory_results_disclose_history_search_failure():
    import memory_tools
    def failure(*args, **kwargs):
        raise OSError("fixture search unavailable")
    ports = SimpleNamespace(store=SimpleNamespace(retrieve=lambda *a, **kw: []),
        sessions=SimpleNamespace(search=failure))
    result = await memory_tools.retrieve_memory(ports, {"query": "prior work"})
    assert "Conversation history could not be searched" in result
    assert "No stored memory" not in result


def test_peer_leader_default_comes_from_proven_native_process(monkeypatch, tmp_path):
    from peers import bridge_api
    native = SimpleNamespace(pid=20, name=lambda: "grok.exe",
        cmdline=lambda: ["grok", "agent", "leader"], environ=lambda: {"GROK_HOME": str(tmp_path)})
    monkeypatch.setattr(bridge_api, "process_matches", lambda *args: True)
    monkeypatch.setattr(bridge_api.psutil, "Process", lambda pid: SimpleNamespace(parents=lambda: [native]))
    metadata = bridge_api._identity({"connection_id": "c", "harness": "grok", "native_session_id": "s",
        "process_id": "30", "runtime_id": "grok:20:1.000000", "runtime_pid": 20,
        "process_started_at": 2, "runtime_started_at": 1, "leader_socket": "attacker.sock"})
    assert metadata["leader_socket"] == str(tmp_path / "leader.sock")
