from __future__ import annotations

import asyncio
import json

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
