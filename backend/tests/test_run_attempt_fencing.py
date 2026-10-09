"""Desktop input is fenced by the admitted attempt, not the resumable run id.

Snapshot Resume keeps the logical run id but runs under a new admission. An
ended attempt stays refused however many other runs end; a resumed
continuation is allowed. Also: a pending macOS permission gate is retired on
shutdown, and late usage failures stay charged to their Goal.
"""

from __future__ import annotations

import threading
import pytest

from desktop_fabric.cua_adapter import CuaDesktopAdapter
from desktop_fabric.cua_macos import MacPermissionRequest
from desktop_fabric.driver_host import CuaDriverHost, CuaRunEnded, session_label
from desktop_fabric.models import WindowRecord
from run_context import Variant1RunContext, bind_run_context
from session_runtime import SessionRuntimeRegistry, SessionRuntimeRepository


class _Client:
    server_info: dict = {}

    def __init__(self):
        self.calls = []
        self.closed = False

    def open(self):
        pass

    def alive(self):
        return not self.closed

    def call_tool(self, name, args):
        self.calls.append((name, dict(args)))
        return {"ok": True}

    def close(self):
        self.closed = True


def _registry(tmp_path):
    return SessionRuntimeRegistry(SessionRuntimeRepository(str(tmp_path / "runtime.sqlite3")))


def test_an_ended_attempt_stays_refused_while_its_resumed_run_may_act(tmp_path):
    registry = _registry(tmp_path)
    client = _Client()
    host = CuaDriverHost.from_client(client)
    host.attempt_active = registry.admission_live

    first = registry.try_reserve_run("chat-a")
    registry.begin_run(first, run_id="graph-run", thread_id="graph-run")
    host.call("click", {}, run_id="graph-run", attempt_id=first)
    registry.finish_run(first, status="interrupted")
    host.end_run("graph-run", first)
    # The driver session of the ended attempt was closed.
    assert ("end_session", {"session": session_label(first)}) in client.calls

    # Snapshot Resume: same logical run id, new admission.
    resumed = registry.try_reserve_run("chat-a")
    registry.begin_run(resumed, run_id="outer-run", thread_id="graph-run")
    host.call("click", {}, run_id="graph-run", attempt_id=resumed)

    # Late input from the first attempt stays refused, even after thousands
    # of other attempts end (nothing is evicted into eligibility).
    for index in range(5000):
        host.end_run(f"other-{index}", f"adm-{index}")
    with pytest.raises(CuaRunEnded):
        host.call("click", {}, run_id="graph-run", attempt_id=first)
    with pytest.raises(CuaRunEnded):
        host.call("get_window_state", {}, run_id="graph-run", attempt_id=first, read_only=True)
    host.call("click", {}, run_id="graph-run", attempt_id=resumed)


def test_without_a_registry_ended_attempts_are_remembered_and_resume_is_allowed():
    host = CuaDriverHost.from_client(_Client())
    host.call("click", {}, run_id="graph-run", attempt_id="adm-1")
    host.end_run("graph-run", "adm-1")
    host.call("click", {}, run_id="graph-run", attempt_id="adm-2")
    with pytest.raises(CuaRunEnded):
        host.call("click", {}, run_id="graph-run", attempt_id="adm-1")


def test_the_adapter_reads_the_attempt_from_the_resumed_graph_context(tmp_path):
    """The real chain: admission metadata -> graph context -> driver call."""

    from agent_engine.execution_context import run_context_from_state
    from agent_engine.presets import chat_task_default

    registry = _registry(tmp_path)
    client = _Client()
    host = CuaDriverHost.from_client(client)
    host.attempt_active = registry.admission_live
    adapter = CuaDesktopAdapter(host=host, platform="linux", started_at=lambda _pid: 1.0,
                                locked=lambda: False)
    window = WindowRecord(window_id="cua:5:7", app_id="cua-app:5", hwnd=7, pid=5,
                          pid_started_at=1.0, bounds=(0, 0, 100, 100))

    first = registry.try_reserve_run("chat-a")
    registry.begin_run(first, run_id="graph-run", thread_id="graph-run")
    registry.finish_run(first, status="interrupted")
    host.end_run("graph-run", first)
    resumed = registry.try_reserve_run("chat-a")
    registry.begin_run(resumed, run_id="outer-run", thread_id="graph-run")

    outer = Variant1RunContext.create(source="chat", run_id="outer-run",
                                      metadata={"admission_id": resumed, "admission_run_id": "outer-run"})
    with bind_run_context(outer):
        graph = run_context_from_state(chat_task_default(), {"run_id": "graph-run"}, runtime=None)
    assert graph.run_id == "graph-run" and graph.metadata["admission_id"] == resumed
    with bind_run_context(graph):
        adapter._tool("click", {"pid": 5, "window_id": 7})
    assert client.calls[-1][1]["session"] == session_label(resumed)

    stale = Variant1RunContext.create(source="chat", run_id="graph-run",
                                      metadata={"admission_id": first})
    with bind_run_context(stale), pytest.raises(Exception, match="ended"):
        adapter._tool("click", {"pid": 5, "window_id": 7})
    assert window.pid == 5


def test_browser_commands_carry_the_attempt_run_used_by_cleanup():
    import asyncio

    from browser_fabric.adapters import EmbeddedBrowserAdapter

    sent = []

    async def request(command):
        sent.append(dict(command))
        return {"ok": True}

    adapter = EmbeddedBrowserAdapter(request=request, owner_chat_id="chat-a")
    graph = Variant1RunContext.create(source="chat", run_id="graph-run",
                                      metadata={"admission_id": "adm-2", "admission_run_id": "outer-run"})
    with bind_run_context(graph):
        asyncio.run(adapter._call("tabs"))
    assert sent[-1]["run_id"] == "outer-run"


def test_shutdown_retires_a_pending_macos_permission_gate():
    opened, released = threading.Event(), threading.Event()
    gates = []

    class Gate:
        def __init__(self, *args, **kwargs):
            self.stopped = threading.Event()
            gates.append(self)

        def launch(self):
            opened.set()

        def wait_listening(self, timeout):
            # Like MacDriverDaemon: stop() ends the wait.
            released.wait(5)
            return False

        def stop(self):
            self.stopped.set()
            released.set()

    request = MacPermissionRequest("/fake/cua-driver", env={}, daemon_factory=Gate)
    host = CuaDriverHost.from_client(_Client())
    host.permissions = request
    assert request.start() and opened.wait(2)
    host.close()
    assert gates[0].stopped.is_set()
    assert not request.active()
    host.close()  # repeated shutdown is harmless
    request.cancel()


def test_a_cancel_during_launch_still_retires_the_gate():
    launching, proceed = threading.Event(), threading.Event()
    gates = []

    class Gate:
        def __init__(self, *args, **kwargs):
            self.stopped = threading.Event()
            self.waited = False
            gates.append(self)

        def launch(self):
            launching.set()
            proceed.wait(5)

        def wait_listening(self, timeout):
            self.waited = True
            return True

        def stop(self):
            self.stopped.set()

    request = MacPermissionRequest("/fake/cua-driver", env={}, daemon_factory=Gate)
    assert request.start() and launching.wait(2)
    canceller = threading.Thread(target=request.cancel)
    canceller.start()
    proceed.set()
    canceller.join(5)
    request._thread.join(5)
    assert gates[0].stopped.is_set()
    assert not request.granted.is_set()


def test_a_late_usage_failure_stays_charged_to_its_goal():
    from llm_manifest_bus import ModelRequestManifestBus

    bus = ModelRequestManifestBus()

    def remember(identity, goal):
        bus._remember_goal({"manifest_id": identity, "run": {"work_scope": {"goal_id": goal}}})

    remember("long-request", "goal-a")
    for index in range(9000):
        remember(f"neighbour-{index}", "goal-b")
        bus.settle_goal(f"neighbour-{index}")
    bus._usage_lost("long-request")
    assert bus.goal_usage_lost("goal-a") == 1
    assert bus.goal_usage_lost("goal-b") == 0
    # A finished request stays attributable while recent; a failure queued
    # with its Goal is charged to it even after it ages out.
    bus.settle_goal("long-request")
    for index in range(9000):
        remember(f"later-{index}", "goal-c")
        bus.settle_goal(f"later-{index}")
    bus._ledger_failed("long-request", True, "goal-a")
    assert bus.goal_usage_lost("goal-a") == 2
    assert bus.goal_usage_lost("goal-c") == 0


def test_goal_attribution_travels_with_the_request_reference():
    """Open requests that never settle cannot push an old request's Goal out."""
    from llm_manifest_bus import ModelRequestManifestBus

    class Ledger:
        def patch_usage(self, *args, **kwargs):
            raise OSError("disk full")

        def get(self, identity):
            return {"goal_id": "goal-stored"} if identity == "stored-request" else None

    bus = ModelRequestManifestBus(usage_ledger=Ledger())
    bus._remember_goal({"manifest_id": "old-request", "run": {"work_scope": {"goal_id": "owner-goal"}}})
    for index in range(65537):
        bus._remember_goal({"manifest_id": f"open-{index}", "run": {"work_scope": {"goal_id": "goal-b"}}})
    assert bus._goal_of("old-request") == ""

    # The adapters' request reference carries the Goal of that exact request.
    bus.patch_usage({"manifest_id": "old-request", "goal_id": "owner-goal"}, {"input_tokens": 1})
    bus.submit_partial_usage("old-request", {"input_tokens": 1}, "owner-goal")
    assert bus.goal_usage_lost("owner-goal") == 2
    assert bus.goal_usage_lost("goal-b") == 0
    # A bare id falls back to the durable request row.
    bus._usage_lost("stored-request")
    assert bus.goal_usage_lost("goal-stored") == 1


def test_the_request_hook_stamps_its_goal_and_the_call_end_settles_it():
    import asyncio
    from types import SimpleNamespace

    from llm_manifest_bus import ModelRequestManifestBus
    from model_runtime.request_manifest import begin_model_call, end_model_call, model_request_event_hooks

    bus = ModelRequestManifestBus()

    class Router:
        _manifest_bus = bus

        async def _record_model_request_manifest(self, manifest):
            await bus.record(manifest)

        def _patch_model_request_manifest_terminal(self, manifest_ref, *, outcome, duration_s=None):
            bus.settle_goal(bus.manifest_id_from_ref(manifest_ref))

    context = Variant1RunContext.create(source="chat", run_id="run-1", work_scope={"goal_id": "goal-a"})

    async def call():
        token = begin_model_call(requested_route="test", selected_mode="test")
        hooks = model_request_event_hooks(
            Router(), provider="openai", api_style="openai", transport="chat_completions",
            adapter="test", adapter_version="1", model="test-model",
            payload={"messages": []}, source_messages=[])
        await hooks["request"][0](SimpleNamespace(content=b'{"messages":[]}'))
        assert bus.goal_id_from_ref(hooks.request_ref) == "goal-a"
        assert bus._inflight_goals == {hooks.request_ref["manifest_id"]: "goal-a"}
        end_model_call(token, outcome="failed")
        return hooks.request_ref["manifest_id"]

    with bind_run_context(context):
        identity = asyncio.run(call())
    assert not bus._inflight_goals
    assert bus._goal_of(identity) == "goal-a"
