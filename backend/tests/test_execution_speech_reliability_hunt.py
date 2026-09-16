from __future__ import annotations

import asyncio
import os
import sys
import threading
from types import SimpleNamespace

import pytest

from artifacts import ContentAddressedArtifactStore
from execution_hosts import ExecutionOwner, ExecutionRepository, ExecutionUnavailable
from execution_hosts.service import TerminalService
from process_tree import CREATE_SUSPENDED
from speech.local_stt import WhisperServer
from work_fabric.scope import WorkScope


def test_terminal_close_reports_pending_while_its_watcher_still_owns_the_pty():
    record = SimpleNamespace(live=True)
    repository = SimpleNamespace(
        get_terminal=lambda _identity: record,
        transition_terminal=lambda *_args, **_kwargs: record,
    )
    terminals = TerminalService(repository, backend_instance_id="test")
    calls = []
    runtime = SimpleNamespace(terminate=lambda **_kwargs: calls.append("terminate"))
    watcher = SimpleNamespace(join=lambda **_kwargs: calls.append("join"), is_alive=lambda: True)
    terminals._live["term-pending"] = runtime
    terminals._watchers["term-pending"] = watcher

    with pytest.raises(ExecutionUnavailable, match="close is still pending"):
        terminals.close("term-pending", force=True)
    assert calls == ["terminate", "join"]


def test_execution_reconcile_scans_beyond_public_1000_row_limit(tmp_path):
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    repository = ExecutionRepository(
        str(tmp_path / "execution.sqlite3"), artifact_store=artifacts,
    )
    owner = ExecutionOwner("chat", "chat-test", WorkScope(chat_id="chat-test"))
    seed = repository.create_terminal(
        terminal_id="term_0000", owner=owner, profile="powershell",
        cwd=str(tmp_path), cols=80, rows=24, transport="conpty",
        capabilities={"true_pty": True}, pid=123, pid_started_at=22.0,
        backend_instance_id="backend-old",
    )
    # Clone only temp test records in a single transaction; this reproduces
    # a long-lived catalog without spawning 1001 OS terminal processes.
    with repository._write() as conn:
        row = conn.execute(
            "SELECT * FROM execution_terminal WHERE terminal_id=?", (seed.terminal_id,),
        ).fetchone()
        columns = [item[1] for item in conn.execute("PRAGMA table_info(execution_terminal)")]
        index = columns.index("terminal_id")
        sql = "INSERT INTO execution_terminal(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")"
        for number in range(1, 1001):
            values = list(row)
            values[index] = f"term_{number:04d}"
            conn.execute(sql, values)
    report = repository.reconcile_stale_backends(
        "backend-new",
        pid_probe=lambda _pid, _started_at: {"identity_matches": False},
        pid_terminator=lambda _pid, _started_at: False,
    )
    assert len(report["terminals"]) == 1001
    assert repository.get_terminal("term_0000").state == "unknown_effect"
    assert repository.get_terminal("term_1000").state == "unknown_effect"


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process admission")
@pytest.mark.asyncio
async def test_whisper_sidecar_is_suspended_until_process_tree_assignment(monkeypatch):
    import speech.local_stt as local_stt

    server = WhisperServer({"binary": "missing.exe", "model": "missing.bin"}, ".")
    monkeypatch.setattr(server, "installed", lambda: True)
    monkeypatch.setattr(server, "_build_args", lambda: ["whisper-server"])
    monkeypatch.setattr(local_stt, "tcp_port_is_free", lambda *_args: True)

    async def healthy(_timeout_s):
        calls.append("healthy")
        return True

    monkeypatch.setattr(server, "_wait_healthy", healthy)
    calls = []
    proc = SimpleNamespace(pid=123, returncode=None)

    async def launch(*_argv, **kwargs):
        calls.append(("launch", kwargs["creationflags"]))
        return proc

    async def own_and_resume(process, owner):
        assert process is proc and owner is None
        calls.append("owned-and-resumed")
        return SimpleNamespace()

    monkeypatch.setattr(local_stt.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(local_stt, "resume_owned_process_and_reap", own_and_resume)
    await server.start()
    assert calls[0][0] == "launch"
    assert calls[0][1] & CREATE_SUSPENDED
    assert calls == [calls[0], "owned-and-resumed", "healthy"]


@pytest.mark.asyncio
async def test_cancelled_tts_worker_does_not_block_voice_metadata(monkeypatch):
    import speech.local_tts as local_tts

    entered = threading.Event()
    release = threading.Event()

    class Engine:
        def create(self, *_args, **_kwargs):
            entered.set()
            release.wait(3)
            return [0.1], 24000

    monkeypatch.setattr(local_tts, "_engine", Engine())
    monkeypatch.setattr(local_tts, "_loaded", True)
    monkeypatch.setattr(local_tts, "_engine_voices", ("af_nova",))
    monkeypatch.setattr(local_tts, "_load_engine", lambda: None)
    monkeypatch.setattr(local_tts, "_wav_bytes", lambda *_args: b"WAV")
    task = asyncio.create_task(local_tts.synthesize("long synthesis"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(
            asyncio.to_thread(local_tts.list_voices), timeout=0.3,
        ) == ["af_nova"]
    finally:
        release.set()


@pytest.mark.asyncio
async def test_cancelled_queued_tts_request_never_starts_stale_synthesis(monkeypatch):
    import speech.local_tts as local_tts

    entered = threading.Event()
    release = threading.Event()
    second_done = threading.Event()
    calls = []

    class Engine:
        def create(self, text, **_kwargs):
            calls.append(text)
            if text == "first":
                entered.set()
                release.wait(3)
            return [0.1], 24000

    monkeypatch.setattr(local_tts, "_engine", Engine())
    monkeypatch.setattr(local_tts, "_loaded", True)
    monkeypatch.setattr(local_tts, "_engine_voices", ("af_nova",))
    monkeypatch.setattr(local_tts, "_load_engine", lambda: None)
    monkeypatch.setattr(local_tts, "_wav_bytes", lambda *_args: b"WAV")
    original_synth = local_tts._synth

    def tracked(text, speed, voice, cancelled):
        try:
            return original_synth(text, speed, voice, cancelled)
        finally:
            if text == "second":
                second_done.set()

    monkeypatch.setattr(local_tts, "_synth", tracked)
    first = asyncio.create_task(local_tts.synthesize("first"))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        second = asyncio.create_task(local_tts.synthesize("second"))
        await asyncio.sleep(0.02)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        assert await first == b"WAV"
        assert await asyncio.to_thread(second_done.wait, 2)
        assert calls == ["first"]
    finally:
        release.set()
        if second is not None and not second.done():
            second.cancel()
