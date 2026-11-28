"""Exercise reader loss with a real stdio pipe and a disposable simulated desktop."""

import sys

import pytest

from artifacts import ContentAddressedArtifactStore
from desktop_fabric import create_desktop_fabric
from desktop_fabric.cua_adapter import CuaDesktopAdapter
from desktop_fabric.cua_client import CuaDriverClient, CuaDriverError
from desktop_fabric.driver_host import CuaDriverHost


_DRIVER = r"""
import json, pathlib, sys, time
root, generation, fault, target = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
for line in sys.stdin:
    msg = json.loads(line)
    method = msg["method"]
    if method.startswith("notifications/"):
        continue
    if method == "initialize":
        result = {"serverInfo": {"name": "fixture", "version": "1"}}
    else:
        name = msg["params"]["name"]
        with (root / "calls").open("a", encoding="utf-8") as log:
            print(generation, name, file=log)
        if name == target and fault == "malformed":
            print("unframed fixture diagnostic", flush=True)
            continue  # The process and its stdin stay alive.
        if name == target and fault == "late":
            deadline = time.monotonic() + 5
            while not (root / "release").exists() and time.monotonic() < deadline:
                time.sleep(.01)
        if name == target and fault == "progress":
            until = time.monotonic() + .7
            while time.monotonic() < until:
                print(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"}), flush=True)
                time.sleep(.015)
        result = {"structuredContent": {"generation": generation, "tool": name, "windows": [{
            "pid": 50, "window_id": 7, "app_name": "Fixture", "x": 0, "y": 0,
            "width": 400, "height": 500}], "tree_markdown": "Fixture", "elements": []}}
        if name == target and fault == "refused":
            result = {"isError": True, "content": [{"type": "text", "text": "fixture refusal"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
"""


@pytest.fixture
def driver(tmp_path):
    hosts = []
    closed = []

    class ObservedClient(CuaDriverClient):
        def close(self):
            if self._process is not None:
                closed.append((self._process, self._process.poll() is None))
            super().close()

    def start(fault, tool, *, every_generation=False):
        clients = []

        def factory(argv):
            generation = len(clients) + 1
            mode = fault if generation == 1 or every_generation else "healthy"
            client = ObservedClient([*argv, str(tmp_path), str(generation), mode, tool], timeout_s=3)
            clients.append(client)
            return client

        host = CuaDriverHost([sys.executable, "-u", "-c", _DRIVER],
                             client_factory=factory, expected_version="1")
        hosts.append(host)
        return host, clients, closed

    yield start
    for host in hosts:
        host.close()
    assert all(process.poll() is not None for process, _ in closed)


@pytest.mark.parametrize("persistent", [False, True])
def test_broken_reader_retries_a_read_on_only_one_fresh_generation(driver, tmp_path, persistent):
    host, clients, closed = driver("malformed", "list_windows", every_generation=persistent)
    if persistent:
        with pytest.raises(CuaDriverError):
            host.call("list_windows", {}, read_only=True)
        assert len(closed) == 2 and not host.status()["running"]
    else:
        assert host.call("list_windows", {}, read_only=True)["generation"] == 2
        assert len(closed) == 1
    assert len(clients) == 2
    assert all(alive and process.poll() is not None for process, alive in closed)
    assert (tmp_path / "calls").read_text().splitlines() == ["1 list_windows", "2 list_windows"]


def test_late_reply_and_application_refusal_keep_the_healthy_connection(driver, tmp_path):
    host, clients, _ = driver("late", "slow_read")
    host.call("first_read", {}, read_only=True)
    clients[0].timeout_s = 0.15
    with pytest.raises(CuaDriverError, match="timed out"):
        host.call("slow_read", {}, read_only=True)
    (tmp_path / "release").touch()
    clients[0].timeout_s = 3
    observed = host.call("next_read", {}, read_only=True)
    assert (observed["generation"], observed["tool"]) == (1, "next_read")
    assert len(clients) == 1
    refused, clients, _ = driver("refused", "missing")
    with pytest.raises(CuaDriverError, match="fixture refusal"):
        refused.call("missing", {}, read_only=True)
    assert refused.call("next_read", {}, read_only=True)["generation"] == 1
    assert len(clients) == 1


def test_progress_notifications_do_not_renew_the_response_timeout(driver):
    host, clients, _ = driver("progress", "busy")
    host.call("first_read", {}, read_only=True)
    clients[0].timeout_s = .12
    with pytest.raises(CuaDriverError, match="timed out"):
        host.call("busy", {}, read_only=True)
    assert len(clients) == 1 and clients[0].alive()


@pytest.mark.asyncio
async def test_lost_input_ack_is_durably_unknown_and_never_replayed(driver, tmp_path):
    host, clients, closed = driver("malformed", "type_text")
    adapter = CuaDesktopAdapter(host=host, platform="win32", started_at=lambda _pid: 1, locked=lambda: False)
    fabric = create_desktop_fabric(path=str(tmp_path / "desktop.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
        adapter=adapter, backend_instance_id="fixture")
    try:
        fabric.refresh_catalog()
        window = "cua:50:7"
        source = await fabric.observe(window, mode="uia", include_image=False)
        args = dict(arguments={"text": "fixture input"}, idempotency_key="one-input",
                    include_image_evidence=False, source_observation=source)
        operation = await fabric.act(window, "type_text", **args)
        saved = fabric.repository.get_operation(operation.operation_id)
        assert saved.state == "unknown_effect" and saved.evidence["automatic_retry"] is False
        assert len(clients) == len(closed) == 1 and closed[0][1]
        assert closed[0][0].poll() is not None and not host.status()["running"]
        assert host.call("list_windows", {}, read_only=True)["generation"] == 2
        same = await fabric.act(window, "type_text", **args)
        assert same.operation_id == saved.operation_id and same.state == saved.state
        assert [row for row in (tmp_path / "calls").read_text().splitlines()
                if row.endswith(" type_text")] == ["1 type_text"]
    finally:
        fabric.close()
