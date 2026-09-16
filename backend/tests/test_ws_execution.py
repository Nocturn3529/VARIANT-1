from types import SimpleNamespace
import asyncio
import sys
import threading

import pytest

from execution_hosts import ExecutionOwner, ExecutionScopeMismatch, create_execution_runtime
from execution_hosts.models import ProcessRecipe
from work_fabric.scope import WorkScope
import ws_execution


def owner(chat="A", generation=1, *, kind="chat", identity=None):
    return ExecutionOwner(kind, identity or chat, WorkScope(
        chat_id=chat, catalog_release_id=f"catalog-{generation}", kernel_generation=generation,
    ))


@pytest.fixture
def runtime(tmp_path):
    value = create_execution_runtime(data_dir=str(tmp_path / "execution"))
    yield value
    value.shutdown()


def handlers():
    result = {}
    def on(*names):
        def register(fn):
            result.update({name: fn for name in names})
            return fn
        return register
    ws_execution.register(on)
    return result


async def command(runtime, kind, chat="A", **fields):
    socket = SimpleNamespace(messages=[])
    async def send(value):
        socket.messages.append(value)
    socket.send_json = send
    sessions = SimpleNamespace(has_session=lambda session_id: session_id in {"A", "B"})
    srv = SimpleNamespace(
        require_runtime=lambda: SimpleNamespace(
            execution=runtime, sessions=sessions,
        ),
    )
    session = SimpleNamespace(viewed_session_id=chat)
    await handlers()[kind](srv, socket, session, {
        "type": kind, "request_id": "request", "chat_id": chat, **fields,
    })
    assert len(socket.messages) == 1
    return socket.messages[0]


def record(runtime, tmp_path, identity, own, kind="process"):
    common = dict(owner=own, pid=0, pid_started_at=0, backend_instance_id="fixture")
    if kind == "process":
        return runtime.repository.create_process(process_id=identity,
            recipe=ProcessRecipe(argv=("fixture",), cwd=str(tmp_path)), **common)
    return runtime.repository.create_terminal(terminal_id=identity, profile="fixture",
        cwd=str(tmp_path), cols=80, rows=24, transport="pipe", capabilities={}, **common)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,operation", [
    ("terminal", "resize"), ("terminal", "read"), ("terminal", "write"),
    ("process", "logs"), ("execution", "get"),
])
async def test_execution_io_does_not_block_websocket_loop(runtime, tmp_path, monkeypatch, kind, operation):
    record(runtime, tmp_path, "terminal", owner(), "terminal")
    record(runtime, tmp_path, "process", owner())
    service = runtime.repository if kind == "execution" else getattr(runtime, kind + ("es" if kind == "process" else "s"))
    method = "list_terminals_for_chat" if kind == "execution" else operation
    original = getattr(service, method)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release, finished = threading.Event(), threading.Event()

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait(2)
        finished.set()
        if operation in {"resize", "write"}:
            return {}
        return original(*args, **kwargs)

    monkeypatch.setattr(service, method, blocked)
    task = asyncio.create_task(command(
        runtime, f"{kind}:{operation}", terminal_id="terminal", process_id="process",
        cols=100, rows=35, data="x",
    ))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not finished.is_set(), "native/storage work blocked the event loop"
    finally:
        release.set()
        response = await task
    assert response["type"] == ("execution:snapshot" if kind == "execution" else f"{kind}:accepted")


def test_identical_terminal_resize_does_not_redraw_or_write_event(runtime, tmp_path):
    runtime.repository.create_terminal(
        terminal_id="sized", owner=owner(), profile="fixture", cwd=str(tmp_path),
        cols=80, rows=24, transport="conpty", capabilities={"resize": True},
        pid=0, pid_started_at=0, backend_instance_id="fixture",
    )
    calls = []
    native = SimpleNamespace(resize=lambda cols, rows: calls.append((cols, rows)) or True)
    runtime.terminals._live["sized"] = native
    try:
        for cols, rows in [(80, 24), (100, 35), (100, 35)]:
            assert runtime.terminals.resize("sized", cols=cols, rows=rows)["supported"]
        assert calls == [(100, 35)]
        events = runtime.repository.list_events_for_chat("A")
        assert sum(row.event_type == "terminal.resized" for row in events) == 1
        before = runtime.terminals.get("sized").revision
        assert runtime.repository.resize_terminal("sized", cols=100, rows=35).revision == before
    finally:
        runtime.terminals._live.pop("sized")


@pytest.mark.parametrize("kind", ["terminal", "process"])
def test_operator_chat_queries_preserve_strict_scope_and_filter_before_limit(runtime, tmp_path, kind):
    repo = runtime.repository
    a = record(runtime, tmp_path, "owned", owner(generation=1), kind)
    goal = record(runtime, tmp_path, "goal", owner(kind="goal", identity="goal-1"), kind)
    record(runtime, tmp_path, "ui-created", ExecutionOwner("chat", "A", WorkScope(chat_id="A")), kind)
    record(runtime, tmp_path, "foreign", owner("B"), kind)
    record(runtime, tmp_path, "inconsistent", owner(identity="B"), kind)
    record(runtime, tmp_path, "unscoped", ExecutionOwner("goal", "unscoped"), kind)
    get = getattr(repo, f"get_{kind}")
    get_for_chat = getattr(repo, f"get_{kind}_for_chat")
    listing = repo.list_processes_for_chat if kind == "process" else repo.list_terminals_for_chat
    with pytest.raises(ExecutionScopeMismatch):
        get("owned", scope=WorkScope(chat_id="A"))
    assert get_for_chat("owned", "A").owner == a.owner
    assert get_for_chat("goal", "A").owner == goal.owner
    assert get_for_chat("ui-created", "A").owner.scope == WorkScope(chat_id="A")
    for identity in ("foreign", "inconsistent", "unscoped"):
        with pytest.raises(ExecutionScopeMismatch):
            get_for_chat(identity, "A")
    with pytest.raises(ExecutionScopeMismatch):
        get_for_chat("owned", "")
    assert listing("", limit=1) == []
    assert len(listing("A", limit=1)) == 1  # Newer foreign records do not hide owned rows.
    assert {getattr(r, f"{kind}_id") for r in listing("A")} == {"owned", "goal", "ui-created"}
    events = repo.list_events_for_chat("A", limit=1)
    assert len(events) == 1 and events[0].entity_id == "owned"
    rest = repo.list_events_for_chat("A", after_sequence=events[0].sequence)
    assert [e.entity_id for e in rest] == ["goal", "ui-created"]
    repo.record_action(kind, "owned", "fixture.after_foreign")
    following = repo.list_events_for_chat("A", after_sequence=rest[-1].sequence, limit=1)
    assert len(following) == 1 and following[0].event_type == "fixture.after_foreign"
    assert repo.list_events_for_chat("") == []


@pytest.mark.asyncio
async def test_native_process_ui_controls_original_generation_by_exact_identity(runtime, tmp_path):
    first = await runtime.start_process(
        [sys.executable, "-u", "-c", "import time; print('ready',flush=True); time.sleep(60)"],
        owner=owner(generation=1), cwd=str(tmp_path),
    )
    second = await runtime.start_process(
        [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
        owner=owner(generation=9), cwd=str(tmp_path),
    )
    foreign = await runtime.start_process(
        [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
        owner=owner("B"), cwd=str(tmp_path),
    )
    assert runtime.processes.get(first.process_id).live
    for operation in ("get", "logs"):
        response = await command(runtime, f"process:{operation}", process_id=first.process_id)
        assert response["type"] == "process:accepted", response
        assert response["chat_id"] == "A"
        assert response["result"]["chat_id"] == "A"
    detached = await command(
        runtime, "process:get", "B", process_id=first.process_id,
        chat_id="A",
    )
    assert detached["type"] == "process:accepted"
    assert detached["chat_id"] == "A"
    rejected = await command(
        runtime, "process:stop", "B", process_id=foreign.process_id,
        chat_id="A", scope=foreign.owner.scope.to_dict(),
    )
    assert rejected["type"] == "process:rejected"
    assert runtime.processes.get(first.process_id).live
    stopped = await command(runtime, "process:stop", process_id=first.process_id)
    assert stopped["type"] == "process:accepted", stopped
    assert stopped["result"]["owner"]["scope"]["kernel_generation"] == 1
    assert not runtime.processes.get(first.process_id).live
    assert runtime.processes.get(second.process_id).live


@pytest.mark.asyncio
async def test_terminal_commands_authorize_before_side_effect(runtime, tmp_path, monkeypatch):
    record(runtime, tmp_path, "terminal", owner(), "terminal")
    calls = []
    monkeypatch.setattr(runtime.terminals, "write", lambda *args: calls.append(args))
    denied = await command(runtime, "terminal:write", "B", terminal_id="terminal", data="input")
    assert denied["type"] == "terminal:rejected" and not calls
    accepted = await command(runtime, "terminal:write", terminal_id="terminal", data="input")
    assert accepted["type"] == "terminal:accepted", accepted
    assert calls == [("terminal", "input")]


@pytest.mark.asyncio
async def test_snapshot_excludes_foreign_events_and_recovery(runtime, tmp_path):
    record(runtime, tmp_path, "owned", owner())
    record(runtime, tmp_path, "foreign", owner("B"))
    runtime.recovery_report = {"terminals": [], "processes": ["owned", "foreign"]}
    response = await command(runtime, "execution:get")
    assert response["chat_id"] == "A"
    assert [r["id"] for r in response["processes"]] == ["owned"]
    assert {r["entity"]["id"] for r in response["events"]} == {"owned"}
    assert response["recovery"]["processes"] == ["owned"]
    empty = await command(runtime, "execution:get", "")
    assert empty["type"] == "execution:rejected"
    assert empty["reason_code"] == "execution_chat_id_required"


def test_unbound_ui_does_not_borrow_global_active_chat():
    srv = SimpleNamespace(require_runtime=lambda: SimpleNamespace(
        sessions=SimpleNamespace(
            get_active=lambda: "another-window",
            has_session=lambda _chat_id: True,
        )))
    with pytest.raises(ValueError, match="chat_id is required"):
        ws_execution._owner(srv, SimpleNamespace(), {})
    assert ws_execution._owner(
        srv, SimpleNamespace(viewed_session_id="B"), {"chat_id": "A"},
    ).owner_id == "A"


def test_terminal_default_cwd_uses_requested_chat_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    sessions = SimpleNamespace(
        get_project=lambda chat_id: {"root": str(project), "name": "project"},
    )
    srv = SimpleNamespace(
        app_root=str(tmp_path), data_dir=str(tmp_path),
        require_runtime=lambda: SimpleNamespace(sessions=sessions),
    )
    assert ws_execution._cwd(srv, "A", None) == str(project.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["terminal", "process"])
@pytest.mark.parametrize("accepted", [True, False])
async def test_signal_reply_preserves_acceptance_and_effect_receipt(runtime, tmp_path, monkeypatch, kind, accepted):
    record(runtime, tmp_path, "owned", owner(), kind)
    service = runtime.terminals if kind == "terminal" else runtime.processes
    receipt = {
        f"{kind}_id": "owned", "signal": "interrupt",
        "transport": "conpty" if kind == "terminal" else "pipe",
        "supported": accepted, "accepted": accepted,
        "status": "accepted" if accepted else "unsupported",
        "effect": "unverified" if accepted else "not_sent",
    }
    if not accepted:
        receipt["reason"] = "No console interrupt is available for this transport."
    calls = []
    monkeypatch.setattr(service, "signal", lambda identity, name: calls.append((identity, name)) or dict(receipt))
    response = await command(runtime, f"{kind}:signal", **{f"{kind}_id": "owned", "name": "interrupt"})
    assert response["type"] == f"{kind}:accepted"
    assert response["chat_id"] == "A"
    assert response["result"]["id"] == "owned"
    assert response["result"]["signal_receipt"] == receipt
    assert calls == [("owned", "interrupt")]
