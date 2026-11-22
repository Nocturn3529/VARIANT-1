from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest

import lifecycle
import server_http


@pytest.mark.asyncio
async def test_lifespan_writes_versioned_instance_handshake(tmp_path):
    port_file = tmp_path / "backend.json"
    runtime = {
        "host": "127.0.0.1",
        "port": 43125,
        "port_file": str(port_file),
        "instance_id": "instance-a",
    }

    async def shutdown():
        return None

    lifespan = lifecycle.make_lifespan(
        runtime=runtime,
        auth_token=lambda: "secret-token",
        activity_token=lambda: "presence-token",
        version="0.1.0",
        start_workers=lambda: [],
        shutdown=shutdown,
    )

    async with lifespan(None):
        payload = json.loads(port_file.read_text(encoding="utf-8"))

    assert payload["port"] == 43125
    assert payload["token"] == "secret-token"
    assert payload["activity_token"] == "presence-token"
    assert payload["activity_token"] != payload["token"]
    assert payload["version"] == "0.1.0"
    assert payload["instance_id"] == "instance-a"
    assert isinstance(payload["pid"], int)
    assert payload["process_started_at"] == psutil.Process(payload["pid"]).create_time()


def test_process_owner_helper_requires_live_executable_args_birth_and_nonce(tmp_path, monkeypatch):
    # A tiny stand-in for server.py keeps the real OS argv and executable
    # checks while leaving the server and all providers uninitialized.
    fixture_server = tmp_path / "server.py"
    fixture_server.write_text("import sys; print('ready', flush=True); sys.stdin.read()")
    monkeypatch.setattr(lifecycle, "__file__", str(tmp_path / "lifecycle.py"))
    port_file = str(tmp_path / "port file.json")
    nonce = "fixture-owner"
    with subprocess.Popen(
        [sys.executable, str(fixture_server), "--port-file", port_file],
        env={**os.environ, "VARIANT1_BACKEND_INSTANCE_ID": nonce},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    ) as child:
        try:
            assert child.stdout.readline() == "ready\n"
            # A Windows venv launcher can stay alive in front of the interpreter.
            launcher = psutil.Process(child.pid)
            candidates = [launcher, *launcher.children(recursive=True)]
            process = next(p for p in candidates if os.path.samefile(p.exe(), psutil.Process().exe()))
            args = [str(process.pid), str(process.create_time()), nonce, port_file]
            assert lifecycle.owns_backend_process(args)
            assert not lifecycle.owns_backend_process([args[0], str(process.create_time() - 1), *args[2:]])
            assert not lifecycle.owns_backend_process([*args[:2], "different", port_file])
            assert not lifecycle.owns_backend_process([*args[:3], str(tmp_path / "other.json")])
            monkeypatch.setattr(lifecycle, "__file__", str(Path(__file__).resolve()))
            assert not lifecycle.owns_backend_process(args)
            assert process.is_running()
        finally:
            child.communicate(timeout=5)


def test_process_owner_cli_exits_quietly_before_server_startup(tmp_path):
    server = Path(__file__).resolve().parents[1] / "server.py"
    candidate = psutil.Process()
    before = set(tmp_path.iterdir())
    result = subprocess.run(
        [sys.executable, str(server), "--check-backend-owner", str(candidate.pid),
         str(candidate.create_time()), "fixture-private-nonce", str(tmp_path / "backend.json")],
        env={**os.environ, "VARIANT1_DATA_DIR": str(tmp_path)},
        capture_output=True, timeout=10,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""
    assert set(tmp_path.iterdir()) == before


@pytest.mark.asyncio
async def test_lifespan_cancels_and_settles_owned_workers():
    started = asyncio.Event()
    settled = asyncio.Event()

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    task = asyncio.create_task(worker())

    async def shutdown():
        return None

    lifespan = lifecycle.make_lifespan(
        runtime={},
        auth_token=lambda: "token",
        version="0.1.0",
        start_workers=lambda: [task],
        shutdown=shutdown,
    )

    async with lifespan(None):
        await started.wait()

    assert task.done()
    assert task.cancelled()
    assert settled.is_set()


def test_health_reports_the_same_backend_instance_identity():
    class Router:
        engine_ready = False
        model_name = ""

    class Host:
        router = Router()
        memory = None
        version = "0.1.0"
        instance_id = "instance-a"
        start_time = 0.0
        startup_ready = True
        startup_error = ""
        startup_failures = []

        @staticmethod
        def engine_label():
            return "llama.cpp"

    payload = server_http.health_payload(Host)

    assert payload["version"] == "0.1.0"
    assert payload["instance_id"] == "instance-a"
    assert payload["status"] == "ok"
    assert payload["ready"] is True
