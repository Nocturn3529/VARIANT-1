from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import execution_hosts.service as execution_service
import execution_hosts.repository as execution_repository
from execution_hosts.local import inherited_environment, run_bounded_child
from execution_hosts.local import PipeTerminalProcess, StructuredChildProcess
from process_tree import OwnedProcessTree

from artifacts import ContentAddressedArtifactStore
from execution_hosts import (
    ExecutionOwner,
    ExecutionNotFound,
    ExecutionRepository,
    ExecutionScopeMismatch,
    ExecutionUnavailable,
    ExecutionValidationError,
    create_execution_runtime,
)
from execution_hosts.windows_conpty import conpty_available
from work_fabric.scope import WorkScope


EXECUTION_DUPLICATE_TOOL_NAMES = {
    "terminal_open", "terminal_list", "terminal_get", "terminal_read",
    "terminal_write", "terminal_resize", "terminal_signal", "terminal_wait",
    "terminal_detach", "terminal_close",
    "process_start", "process_list", "process_get", "process_logs",
    "process_write", "process_signal", "process_wait", "process_stop",
}


def _runtime(tmp_path: Path, *, spool: int = 4096, **kwargs):
    return create_execution_runtime(
        data_dir=str(tmp_path / "runtime"), live_spool_bytes=spool,
        **kwargs,
    )


def _owner() -> ExecutionOwner:
    return ExecutionOwner(
        "conversation", "conversation-1",
        WorkScope(
            chat_id="chat-1", conversation_id="conversation-1",
            branch_id="branch-1", workspace_id="workspace-1",
            workspace_revision=7, worktree_id="worktree-2",
        ),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended Job admission")
@pytest.mark.parametrize("kind", ["pipe_terminal", "structured_process"])
def test_pipe_process_cannot_execute_before_job_assignment(tmp_path, monkeypatch, kind):
    marker = tmp_path / "child-effect.txt"
    entered = threading.Event()
    release = threading.Event()
    real_assign = OwnedProcessTree.assign
    observed = {}

    def delayed_assign(self, process):
        entered.set()
        assert release.wait(5), "test never released Job assignment"
        assert not marker.exists(), "child ran before Job assignment"
        return real_assign(self, process)

    monkeypatch.setattr(OwnedProcessTree, "assign", delayed_assign)

    def spawn():
        try:
            argv = [
                sys.executable, "-c",
                "from pathlib import Path; import time; "
                f"Path({str(marker)!r}).write_text('ran'); time.sleep(2)",
            ]
            if kind == "pipe_terminal":
                observed["child"] = PipeTerminalProcess(
                    argv, cwd=str(tmp_path), env=inherited_environment(),
                    on_output=lambda *_: None, degraded_reason="test",
                )
            else:
                observed["child"] = StructuredChildProcess(
                    argv, cwd=str(tmp_path), env=inherited_environment(),
                    shell=False, on_output=lambda *_: None,
                )
        except BaseException as exc:
            observed["error"] = exc

    thread = threading.Thread(target=spawn, daemon=True)
    thread.start()
    try:
        assert entered.wait(5), "spawn did not reach Job assignment"
        assert not marker.exists()
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        if "error" in observed:
            raise observed["error"]
        for _ in range(200):
            if marker.exists():
                break
            time.sleep(0.01)
        assert marker.read_text() == "ran"
    finally:
        release.set()
        child = observed.get("child")
        if child is not None:
            child.terminate()
            child.close()
        thread.join(5)


def _launcher_handoff(tmp_path: Path, *, exit_code: int = 0):
    """A child cannot finish its real effect until the test releases it."""
    child = tmp_path / 'handoff_child.py'
    child.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        "root=Path(sys.argv[1])\n"
        "(root/'child-ready').write_text(str(os.getpid()))\n"
        "deadline=time.monotonic()+15\n"
        "while not (root/'release-child').exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.02)\n"
        "if (root/'release-child').exists():\n"
        "    (root/'child-effect').write_text('finished')\n"
        "    print('child-effect-finished',flush=True)\n",
        encoding='utf-8',
    )
    launcher = tmp_path / 'handoff_launcher.py'
    launcher.write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "root=Path(sys.argv[1])\n"
        "marker=root/'launch-count'\n"
        "marker.write_text(str(int(marker.read_text())+1 if marker.exists() else 1))\n"
        "subprocess.Popen([sys.executable,str(root/'handoff_child.py'),str(root)],"
        "stdin=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))\n"
        "deadline=time.monotonic()+5\n"
        "while not (root/'child-ready').exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.02)\n"
        "assert (root/'child-ready').exists()\n"
        f"sys.exit({exit_code})\n",
        encoding='utf-8',
    )
    return [sys.executable, '-u', str(launcher), str(tmp_path)]


def _wait_handoff(runtime, identity: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = runtime.processes.get(identity)
        if record.health.get('leader_alive') is False:
            return record
        if not record.live:
            pytest.fail(f'Owned work was prematurely finalized: {record}')
        time.sleep(0.02)
    pytest.fail('Launcher did not report its live owned descendants')


@pytest.mark.parametrize('mode', ['once', 'process'])
@pytest.mark.parametrize('settlement', ['natural', 'stop', 'shutdown'])
def test_launcher_handoff_stays_owned_and_controllable(tmp_path, mode, settlement):
    import psutil

    runtime = _runtime(tmp_path)
    argv = _launcher_handoff(tmp_path)
    try:
        if mode == 'once':
            result = runtime.processes.run_bounded(
                argv, owner=_owner(), cwd=str(tmp_path), timeout=3,
            )
            assert not result.timed_out and not result.cancelled
            assert result.process.exit_code == 0
            process = result.process
        else:
            process = runtime.processes.start(argv, owner=_owner(), cwd=str(tmp_path))
        waiting = _wait_handoff(runtime, process.process_id)
        child_pid = int((tmp_path/'child-ready').read_text())
        assert psutil.pid_exists(child_pid)
        assert waiting.live and waiting.health['owned_descendant_count'] >= 1
        assert waiting.health['launcher_exit_code'] == 0
        assert runtime.processes.wait(process.process_id, condition='exit', timeout=0).live
        assert not (tmp_path/'child-effect').exists()
        # Health inspection must not discard the command/tree distinction.
        healthy = runtime.processes.wait(process.process_id, condition='healthy', timeout=0)
        assert healthy.health['leader_alive'] is False
        assert healthy.exit_code == 0

        if settlement == 'natural':
            (tmp_path/'release-child').touch()
            settled = runtime.processes.wait(process.process_id, condition='exit', timeout=5)
            assert settled.state == 'exited'
            assert (tmp_path/'child-effect').read_text() == 'finished'
            assert 'child-effect-finished' in _page_text(runtime.processes.logs(process.process_id))
        elif settlement == 'stop':
            settled = runtime.processes.stop(process.process_id)
            assert settled.state == 'terminated'
        else:
            runtime.processes.shutdown(terminate_live=True)
            assert runtime.processes.get(process.process_id).state == 'terminated'
        assert not psutil.pid_exists(child_pid)
        if settlement != 'natural':
            (tmp_path/'release-child').touch()
            assert not (tmp_path/'child-effect').exists()
    finally:
        runtime.shutdown()


@pytest.mark.parametrize('policy,exit_code', [('always', 0), ('on_failure', 7)])
def test_launcher_restart_waits_for_owned_children(tmp_path, policy, exit_code):
    runtime = _runtime(tmp_path)
    try:
        started = runtime.processes.start(
            _launcher_handoff(tmp_path, exit_code=exit_code), owner=_owner(),
            cwd=str(tmp_path), restart=policy, max_attempts=2, restart_delay_s=0.02,
        )
        waiting = _wait_handoff(runtime, started.process_id)
        assert waiting.attempt == 1
        assert (tmp_path/'launch-count').read_text() == '1'
        assert 'process.restart_scheduled' not in {
            e.event_type for e in runtime.events(entity_kind='process', entity_id=started.process_id)
        }
        (tmp_path/'release-child').touch()
        settled = runtime.processes.wait(started.process_id, condition='exit', timeout=8)
        assert not settled.live and settled.attempt == 2
        assert settled.exit_code == exit_code
        assert (tmp_path/'launch-count').read_text() == '2'
        assert sum(e.event_type == 'process.restart_scheduled' for e in runtime.events(
            entity_kind='process', entity_id=started.process_id)) == 1
    finally:
        runtime.shutdown()


def _page_text(page) -> str:
    return "".join(frame.data.decode("utf-8", errors="replace") for frame in page.frames)


def _wait_text(read, identity: str, needle: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = read(identity, after_cursor=0, max_bytes=1024 * 1024)
        if needle in _page_text(last):
            return last
        time.sleep(0.03)
    raise AssertionError(f"output never contained {needle!r}: {last}")


@pytest.mark.parametrize("kind", ["process", "pipe_terminal", "terminal"])
@pytest.mark.parametrize("ignore", [False, True])
def test_signal_acceptance_is_separate_from_observed_effect(tmp_path, kind, ignore):
    if kind == "terminal" and os.name == "nt" and not conpty_available():
        pytest.skip("ConPTY unavailable")
    runtime = _runtime(tmp_path)
    marker = tmp_path / "signal-observed.txt"
    source = (
        "import signal,time,sys\nfrom pathlib import Path\n"
        "def received(*args):\n"
        + ("    pass\n" if ignore else f"    Path({str(marker)!r}).write_text('SIGINT')\n    sys.exit(0)\n")
        + "signal.signal(signal.SIGINT, received)\n"
        "if hasattr(signal, 'SIGBREAK'): signal.signal(signal.SIGBREAK, received)\n"
        "print('HANDLER_READY',flush=True)\nwhile True: time.sleep(.05)\n"
    )
    try:
        if kind == "process":
            service = runtime.processes
            record = service.start([sys.executable, "-u", "-c", source], owner=_owner(), cwd=str(tmp_path))
            identity, read = record.process_id, service.logs
        else:
            service = runtime.terminals
            record = service.open(owner=_owner(), cwd=str(tmp_path), profile="custom",
                                  argv=[sys.executable, "-u", "-c", source],
                                  force_pipe_fallback=kind == "pipe_terminal")
            identity, read = record.terminal_id, service.read
        _wait_text(read, identity, "HANDLER_READY")
        receipt = service.signal(identity)
        unsupported = os.name == "nt" and kind != "terminal"
        assert receipt["supported"] is not unsupported
        assert receipt["accepted"] is not unsupported
        assert receipt["effect"] == ("not_sent" if unsupported else "unverified")
        assert "delivered" not in receipt
        if unsupported or ignore:
            time.sleep(.2)
            assert not marker.exists()
            assert service.get(identity).live
        else:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.03)
            assert marker.read_text() == "SIGINT"
        events = runtime.repository.list_events(entity_id=identity)
        assert any(event.event_type.endswith("signal_" + receipt["status"]) for event in events)
        assert not any(event.event_type.endswith("signal_delivered") for event in events)
    finally:
        runtime.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY raw-input integration")
def test_conpty_ctrl_c_reaches_raw_tui_without_claiming_exit(tmp_path):
    if not conpty_available():
        pytest.skip("ConPTY unavailable")
    runtime = _runtime(tmp_path)
    marker = tmp_path / "unexpected-sigint.txt"
    source = (
        "import ctypes,msvcrt,signal,sys\n"
        "from ctypes import wintypes\nfrom pathlib import Path\n"
        "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "k.GetStdHandle.argtypes=[wintypes.DWORD]\n"
        "k.GetStdHandle.restype=wintypes.HANDLE\n"
        "k.GetConsoleMode.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]\n"
        "k.SetConsoleMode.argtypes=[wintypes.HANDLE,wintypes.DWORD]\n"
        "handle=k.GetStdHandle(-10)\nmode=wintypes.DWORD()\n"
        "assert k.GetConsoleMode(handle,ctypes.byref(mode))\n"
        "assert k.SetConsoleMode(handle,mode.value & ~7)\n"
        f"signal.signal(signal.SIGINT,lambda *_: Path({str(marker)!r}).write_text('SIGINT'))\n"
        "print('RAW_TUI_READY',flush=True)\n"
        "while True:\n"
        "    key=msvcrt.getwch()\n"
        "    if key=='\\x03': print('ETX_INPUT_APP_STILL_RUNNING',flush=True)\n"
        "    elif key=='q': break\n"
    )
    try:
        terminal = runtime.terminals.open(
            owner=_owner(), cwd=str(tmp_path), profile="custom",
            argv=[sys.executable, "-X", "utf8", "-u", "-c", source],
        )
        identity = terminal.terminal_id
        assert terminal.transport == "conpty"
        _wait_text(runtime.terminals.read, identity, "RAW_TUI_READY")
        receipt = runtime.terminals.signal(identity)
        assert receipt["accepted"] and receipt["supported"]
        assert receipt["delivery"] == "terminal_input"
        assert receipt["effect"] == "unverified"
        _wait_text(runtime.terminals.read, identity, "ETX_INPUT_APP_STILL_RUNNING")
        assert runtime.terminals.get(identity).live
        assert not marker.exists()
        # Output reads never become process controls. Explicit close does.
        runtime.terminals.read(identity)
        assert runtime.terminals.get(identity).live
        runtime.terminals.close(identity)
        assert not runtime.terminals.get(identity).live
    finally:
        runtime.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="Windows CTRL+C ignore bit")
def test_host_ctrl_c_ignore_bit_tracks_the_console_handler():
    import ctypes
    from ctypes import wintypes

    from execution_hosts.windows_conpty import _host_ignores_ctrl_c

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    was = _host_ignores_ctrl_c()
    try:
        assert kernel32.SetConsoleCtrlHandler(None, True)
        assert _host_ignores_ctrl_c() is True
        assert kernel32.SetConsoleCtrlHandler(None, False)
        assert _host_ignores_ctrl_c() is False
    finally:
        assert kernel32.SetConsoleCtrlHandler(None, bool(was))


@pytest.mark.parametrize("written,accepted", [(0, False), (1, True)])
def test_conpty_signal_requires_the_control_byte_to_be_written(written, accepted):
    from execution_hosts.windows_conpty import WindowsConPtyProcess
    writes = []
    target = SimpleNamespace(write=lambda data: writes.append(data) or written)
    assert WindowsConPtyProcess.signal(target, "interrupt") is accepted
    assert writes == [b"\x03"]


def test_bounded_child_caps_output_while_draining_both_pipes(tmp_path):
    result = run_bounded_child(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys,threading;"
            "t=threading.Thread(target=lambda: sys.stderr.buffer.write(b'e'*1048576));"
            "t.start(); sys.stdout.buffer.write(b'o'*1048576); t.join()",
        ],
        cwd=str(tmp_path),
        env=inherited_environment(),
        timeout=10,
        max_stdout_bytes=4096,
        max_stderr_bytes=2048,
    )

    assert result.output_limit_exceeded is True
    assert result.stdout_limit_exceeded is True
    assert result.stderr_limit_exceeded is True
    assert len(result.stdout) == 4096
    assert len(result.stderr) == 2048
    assert result.timed_out is False


def test_execution_capabilities_register_only_the_remote_handle_router():
    from execution_hosts.capabilities import register_execution_tools
    from tools import ToolRegistry

    host = SimpleNamespace(registry=ToolRegistry(), remote_handle_routers={})
    register_execution_tools(host)

    assert "execution" in host.remote_handle_routers
    registered = {tool.name for tool in host.registry.all()}
    assert EXECUTION_DUPLICATE_TOOL_NAMES.isdisjoint(registered)
    assert registered == set()


def test_execution_repository_filters_scope_before_limits_and_events(tmp_path):
    runtime = _runtime(tmp_path)
    wanted_owner = _owner()
    wanted = runtime.repository.create_terminal(
        terminal_id="terminal-wanted",
        owner=wanted_owner,
        profile="default",
        cwd=str(tmp_path),
        cols=80,
        rows=24,
        transport="pipe",
        capabilities={},
        pid=1,
        pid_started_at=1,
        backend_instance_id="test",
    )
    for index in range(205):
        runtime.repository.create_terminal(
            terminal_id=f"terminal-foreign-{index:03d}",
            owner=ExecutionOwner(
                "chat", f"foreign-{index}",
                WorkScope(chat_id=f"foreign-{index}", workspace_id="other"),
            ),
            profile="default",
            cwd=str(tmp_path),
            cols=80,
            rows=24,
            transport="pipe",
            capabilities={},
            pid=index + 2,
            pid_started_at=1,
            backend_instance_id="test",
        )

    scoped = runtime.terminals.list(scope=wanted_owner.scope, limit=200)
    events = runtime.events(scope=wanted_owner.scope, limit=500)
    assert [row.terminal_id for row in scoped] == [wanted.terminal_id]
    assert len(events) == 1
    assert events[0].entity_id == wanted.terminal_id
    assert events[0].payload["cwd"] == str(tmp_path)
    with pytest.raises(ExecutionScopeMismatch):
        runtime.terminals.get(
            wanted.terminal_id,
            scope=WorkScope(chat_id="chat-1", branch_id="another-branch"),
        )


def test_live_worktree_owner_query_is_uncapped_and_exact(tmp_path):
    runtime = _runtime(tmp_path)
    target = "worktree-owned"
    for index in range(205):
        runtime.repository.create_terminal(
            terminal_id=f"terminal-unrelated-{index:03d}",
            owner=ExecutionOwner(
                "chat", f"chat-{index}", WorkScope(worktree_id=f"other-{index}"),
            ),
            profile="default", cwd=str(tmp_path), cols=80, rows=24,
            transport="pipe", capabilities={}, pid=index + 1,
            pid_started_at=1, backend_instance_id="test",
        )
    runtime.repository.create_terminal(
        terminal_id="terminal-worktree-owner",
        owner=ExecutionOwner("chat", "owner", WorkScope(worktree_id=target)),
        profile="default", cwd=str(tmp_path), cols=80, rows=24,
        transport="pipe", capabilities={}, pid=9999,
        pid_started_at=1, backend_instance_id="test",
    )

    assert runtime.worktree_has_live_owner(target) is True
    assert runtime.worktree_has_live_owner("missing") is False


def test_structured_process_has_exact_owner_cursor_logs_and_durable_events(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        record = runtime.processes.start(
            [
                sys.executable, "-u", "-c",
                'import sys; print("stdout-one"); print("stderr-two", file=sys.stderr)',
            ],
            owner=_owner(), cwd=str(tmp_path),
        )
        assert record.owner.scope.workspace_revision == 7
        assert record.owner.scope.worktree_id == "worktree-2"
        finished = runtime.processes.wait(record.process_id, timeout=10)
        assert finished.state == "exited"
        assert finished.exit_code == 0

        first = runtime.processes.logs(record.process_id, max_bytes=8)
        assert first.next_cursor == 8
        assert first.more is True
        second = runtime.processes.logs(
            record.process_id, after_cursor=first.next_cursor, max_bytes=1024)
        combined = _page_text(first) + _page_text(second)
        assert "stdout-one" in combined
        assert "stderr-two" in combined
        assert second.next_cursor == second.end_cursor

        events = runtime.events(entity_kind="process", entity_id=record.process_id)
        assert [event.event_type for event in events] == [
            "process.dispatch_reserved", "process.started", "process.exited",
        ]
        durable = runtime.processes.get(record.process_id)
        assert durable.owner.scope.workspace_id == "workspace-1"
    finally:
        runtime.shutdown()


def test_exit_waits_for_structured_process_trailing_output(tmp_path):
    runtime = _runtime(tmp_path, spool=2 * 1024 * 1024)
    source = (
        "import sys\n"
        "sys.stdout.buffer.write(b'x' * 500000 + b'PROCESS_TAIL\\n')\n"
        "sys.stderr.buffer.write(b'y' * 250000 + b'PROCESS_ERR_TAIL\\n')\n"
        "sys.stdout.flush(); sys.stderr.flush()\n"
    )
    try:
        process = runtime.processes.start(
            [sys.executable, "-u", "-c", source],
            owner=_owner(),
            cwd=str(tmp_path),
        )

        finished = runtime.processes.wait(process.process_id, timeout=10)
        page = runtime.processes.logs(
            process.process_id,
            max_bytes=1024 * 1024,
            max_frames=10_000,
        )
        text = _page_text(page)

        assert finished.state == "exited"
        assert "PROCESS_TAIL" in text
        assert "PROCESS_ERR_TAIL" in text
        assert page.next_cursor == page.end_cursor
    finally:
        runtime.shutdown()


def test_exit_waits_for_pipe_terminal_trailing_output(tmp_path):
    runtime = _runtime(tmp_path, spool=2 * 1024 * 1024)
    source = (
        "import sys\n"
        "sys.stdout.buffer.write(b'z' * 500000 + b'TERMINAL_TAIL\\n')\n"
        "sys.stdout.flush()\n"
    )
    try:
        terminal = runtime.terminals.open(
            owner=_owner(),
            cwd=str(tmp_path),
            profile="custom",
            argv=[sys.executable, "-u", "-c", source],
            force_pipe_fallback=True,
        )

        finished = runtime.terminals.wait(terminal.terminal_id, timeout=10)
        page = runtime.terminals.read(
            terminal.terminal_id,
            max_bytes=1024 * 1024,
            max_frames=10_000,
        )

        assert finished.state == "exited"
        assert "TERMINAL_TAIL" in _page_text(page)
        assert page.next_cursor == page.end_cursor
    finally:
        runtime.shutdown()


@pytest.mark.asyncio
async def test_chat_deletion_settles_all_live_execution_owners(tmp_path):
    runtime = _runtime(tmp_path)
    owner = ExecutionOwner("chat", "chat-delete", WorkScope(chat_id="chat-delete"))
    other = ExecutionOwner("chat", "chat-keep", WorkScope(chat_id="chat-keep"))
    source = "import time; time.sleep(30)"
    try:
        process = runtime.processes.start(
            [sys.executable, "-c", source], owner=owner, cwd=str(tmp_path),
        )
        terminal = runtime.terminals.open(
            owner=owner, cwd=str(tmp_path), profile="custom",
            argv=[sys.executable, "-c", source], force_pipe_fallback=True,
        )
        kept = runtime.processes.start(
            [sys.executable, "-c", source], owner=other, cwd=str(tmp_path),
        )

        closed = await runtime.delete_chat("chat-delete")

        assert closed == 2
        assert runtime.processes.get(process.process_id).live is False
        assert runtime.terminals.get(terminal.terminal_id).live is False
        assert runtime.processes.get(kept.process_id).live is True
    finally:
        runtime.shutdown()


def test_process_dispatch_is_durable_before_os_spawn(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    process_id = "proc_durable_dispatch_fence"
    original_spawn = runtime.processes._spawn_runtime
    observed = []

    def fenced_spawn(identity, recipe, registered):
        reserved = runtime.processes.repository.get_process(identity)
        observed.append((reserved.state, reserved.pid, reserved.recovery))
        return original_spawn(identity, recipe, registered)

    monkeypatch.setattr(runtime.processes, "_spawn_runtime", fenced_spawn)
    try:
        record = runtime.processes.start(
            [sys.executable, "-c", "print('reserved')"],
            owner=_owner(),
            cwd=str(tmp_path),
            process_id=process_id,
        )
        assert record.process_id == process_id
        assert observed == [
            ("starting", 0, {"dispatch_reserved": True})
        ]
        replay = runtime.processes.start(
            [sys.executable, "-c", "print('reserved')"],
            owner=_owner(),
            cwd=str(tmp_path),
            process_id=process_id,
        )
        assert replay.process_id == process_id
        assert len(observed) == 1
    finally:
        runtime.shutdown()


def test_terminal_shutdown_cancels_pending_spawn_and_reaps_late_child(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    shutdown_done = threading.Event()
    opened = []
    errors = []
    children = []
    original = execution_service.spawn_terminal

    def gated_spawn(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(execution_service, "spawn_terminal", gated_spawn)

    def open_terminal():
        try:
            opened.append(runtime.terminals.open(
                owner=_owner(),
                cwd=str(tmp_path),
                argv=[sys.executable, "-u", "-c", "import time; time.sleep(30)"],
                force_pipe_fallback=True,
            ))
        except BaseException as exc:
            errors.append(exc)

    opener = threading.Thread(target=open_terminal, daemon=True)
    closer = threading.Thread(
        target=lambda: (
            runtime.terminals.shutdown(terminate_live=True), shutdown_done.set()
        ),
        daemon=True,
    )
    opener.start()
    assert entered.wait(timeout=5)
    closer.start()
    assert not shutdown_done.wait(timeout=0.05)
    release.set()
    opener.join(timeout=10)
    closer.join(timeout=10)
    try:
        assert len(errors) == 1 and isinstance(errors[0], execution_service._StartCancelled)
        assert not opened
        assert children and children[0].wait(timeout=0) is not None
        assert shutdown_done.is_set()
        assert runtime.terminals._live == {}
    finally:
        release.set()
        runtime.shutdown()


def test_process_shutdown_cancels_pending_spawn_and_reaps_late_child(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    shutdown_done = threading.Event()
    started = []
    errors = []
    children = []
    original = runtime.processes._spawn_runtime

    def gated_spawn(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(runtime.processes, "_spawn_runtime", gated_spawn)

    def start_process():
        try:
            started.append(runtime.processes.start(
                [sys.executable, "-u", "-c", "import time; time.sleep(30)"],
                owner=_owner(),
                cwd=str(tmp_path),
            ))
        except BaseException as exc:
            errors.append(exc)

    starter = threading.Thread(target=start_process, daemon=True)
    closer = threading.Thread(
        target=lambda: (
            runtime.processes.shutdown(terminate_live=True), shutdown_done.set()
        ),
        daemon=True,
    )
    starter.start()
    assert entered.wait(timeout=5)
    closer.start()
    assert not shutdown_done.wait(timeout=0.05)
    release.set()
    starter.join(timeout=10)
    closer.join(timeout=10)
    try:
        assert len(errors) == 1 and isinstance(errors[0], execution_service._StartCancelled)
        assert not started
        assert children and children[0].wait(timeout=0) is not None
        assert shutdown_done.is_set()
        assert runtime.processes._live == {}
    finally:
        release.set()
        runtime.shutdown()


def test_bounded_live_spool_rolls_exact_cursor_ranges_to_shared_cas(tmp_path):
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    repository = ExecutionRepository(
        str(tmp_path / "execution.sqlite3"), artifact_store=artifacts,
        live_spool_bytes=64,
    )
    terminal = repository.create_terminal(
        terminal_id="term-spool", owner=_owner(), profile="fake",
        cwd=str(tmp_path), cols=80, rows=24, transport="test",
        capabilities={"true_pty": False}, pid=42, pid_started_at=1,
        backend_instance_id="backend-a",
    )
    first_payload = b"a" * 80
    second_payload = b"b" * 30
    assert repository.append_output(
        "terminal", terminal.terminal_id, "terminal", first_payload) == (0, 80)
    assert repository.append_output(
        "terminal", terminal.terminal_id, "terminal", second_payload) == (80, 110)

    current = repository.get_terminal(terminal.terminal_id)
    assert current.output_cursor == 110
    assert current.live_start_cursor == 80
    page = repository.read_output(
        "terminal", terminal.terminal_id, after_cursor=0, max_bytes=10,
        prefer_artifact_refs=True,
    )
    assert page.frames[0].start_cursor == 0
    assert page.frames[0].end_cursor == 80
    assert page.frames[0].artifact_ref.startswith("artifact://sha256/")
    assert artifacts.read_bytes_scoped(
        page.frames[0].artifact_ref, "execution:terminal:term-spool") == first_payload
    tail = repository.read_output(
        "terminal", terminal.terminal_id, after_cursor=80, max_bytes=100)
    assert _page_text(tail) == "b" * 30
    assert tail.next_cursor == 110


def test_many_short_output_frames_remain_lossless_across_repeated_compaction(tmp_path):
    repository = ExecutionRepository(
        str(tmp_path / "execution.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
        live_spool_bytes=128,
    )
    repository.create_terminal(
        terminal_id="frames", owner=_owner(), profile="fake", cwd=str(tmp_path),
        cols=80, rows=24, transport="test", capabilities={}, pid=0, pid_started_at=0,
        backend_instance_id="fixture",
    )
    expected = b""
    for index in range(120):
        data = bytes([index]) * (index % 23 + 1)
        start, end = repository.append_output("terminal", "frames", "terminal", data)
        assert start == len(expected) and end == start + len(data)
        expected += data
        current = repository.get_terminal("frames")
        assert 0 <= current.output_cursor - current.live_start_cursor <= 128
    observed, cursor = b"", 0
    while cursor < len(expected):
        page = repository.read_output("terminal", "frames", after_cursor=cursor,
                                      max_bytes=47, max_frames=7)
        observed += b"".join(frame.data for frame in page.frames)
        assert page.next_cursor > cursor
        cursor = page.next_cursor
    assert observed == expected


def test_output_page_keeps_one_snapshot_during_concurrent_archiving(tmp_path, monkeypatch):
    repository = ExecutionRepository(
        str(tmp_path / "execution.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
        live_spool_bytes=128,
    )
    repository.create_terminal(
        terminal_id="snapshot", owner=_owner(), profile="fake", cwd=str(tmp_path),
        cols=80, rows=24, transport="test", capabilities={}, pid=0, pid_started_at=0,
        backend_instance_id="fixture",
    )
    repository.append_output("terminal", "snapshot", "terminal", b"initial output")
    read = repository._read
    archived = False

    @contextmanager
    def racing_read():
        with read() as conn:
            class Connection:
                def execute(self, sql, args=()):
                    nonlocal archived
                    cursor = conn.execute(sql, args)
                    if "FROM execution_output_segment" in sql and not archived:
                        rows = cursor.fetchall()
                        archived = True
                        # Writer commits after archive query, before live query.
                        repository.append_output("terminal", "snapshot", "terminal", b"z" * 140)
                        return SimpleNamespace(fetchall=lambda: rows)
                    return cursor
            yield Connection()

    monkeypatch.setattr(repository, "_read", racing_read)
    page = repository.read_output("terminal", "snapshot", max_bytes=1024)
    assert archived
    assert b"".join(frame.data for frame in page.frames) == b"initial output"
    assert page.end_cursor == page.next_cursor == len(b"initial output")


def test_pipe_fallback_is_reconnectable_but_never_claimed_as_pty(tmp_path):
    runtime = _runtime(tmp_path)
    source = (
        "import sys\n"
        "print('READY', flush=True)\n"
        "for line in sys.stdin:\n"
        " print('ECHO:' + line.rstrip(), flush=True)\n"
    )
    try:
        first = runtime.terminals.open(
            owner=_owner(), cwd=str(tmp_path), profile="custom",
            argv=[sys.executable, "-u", "-c", source],
            force_pipe_fallback=True,
        )
        second = runtime.terminals.open(
            owner=_owner(), cwd=str(tmp_path), profile="custom",
            argv=[sys.executable, "-u", "-c", source],
            force_pipe_fallback=True,
        )
        assert first.terminal_id != second.terminal_id
        assert first.transport == "pipe_fallback"
        assert first.capabilities["true_pty"] is False
        assert first.capabilities["resize"] is False
        assert "degraded_reason" in first.capabilities
        _wait_text(runtime.terminals.read, first.terminal_id, "READY")
        runtime.terminals.write(first.terminal_id, "hello\n")
        page = _wait_text(runtime.terminals.read, first.terminal_id, "ECHO:hello")
        cursor = page.end_cursor

        detached = runtime.terminals.detach(first.terminal_id)
        assert detached.attachments == 0
        attached = runtime.terminals.attach(first.terminal_id)
        assert attached.attachments == 1
        replay = runtime.terminals.read(first.terminal_id, after_cursor=0)
        assert replay.end_cursor >= cursor
        assert "ECHO:hello" in _page_text(replay)
        assert runtime.terminals.resize(
            first.terminal_id, cols=90, rows=20)["supported"] is False
        assert runtime.terminals.get(first.terminal_id).capabilities["true_pty"] is False
    finally:
        runtime.shutdown()


def test_ten_terminal_sessions_coexist_with_distinct_handles(tmp_path):
    runtime = _runtime(tmp_path)
    source = (
        "import sys,time\n"
        "print(sys.argv[1], flush=True)\n"
        "time.sleep(10)\n"
    )
    try:
        records = [runtime.terminals.open(
            owner=_owner(), cwd=str(tmp_path), profile="custom",
            argv=[sys.executable, "-u", "-c", source, f"SESSION_{index}"],
            force_pipe_fallback=True,
        ) for index in range(10)]
        assert len({record.terminal_id for record in records}) == 10
        for index, record in enumerate(records):
            page = _wait_text(
                runtime.terminals.read, record.terminal_id, f"SESSION_{index}")
            assert f"SESSION_{index}" in _page_text(page)
        assert len(runtime.terminals.list(owner_id="conversation-1")) == 10
    finally:
        runtime.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY integration")
def test_windows_conpty_is_real_when_kernel_exports_are_available(tmp_path):
    if not conpty_available():
        pytest.skip("Windows build has no ConPTY exports")
    runtime = _runtime(tmp_path)
    source = (
        "import sys\n"
        "print('PTY_READY', flush=True)\n"
        "for line in sys.stdin:\n"
        " print('PTY_ECHO:' + line.rstrip(), flush=True)\n"
    )
    try:
        terminal = runtime.terminals.open(
            owner=_owner(), cwd=str(tmp_path), profile="custom",
            argv=[sys.executable, "-u", "-c", source],
        )
        assert terminal.transport == "conpty"
        assert terminal.capabilities["true_pty"] is True
        assert terminal.capabilities["ansi"] is True
        assert terminal.capabilities["backend_restart_survival"] is False
        _wait_text(runtime.terminals.read, terminal.terminal_id, "PTY_READY")
        runtime.terminals.write(terminal.terminal_id, "world\r\n")
        _wait_text(runtime.terminals.read, terminal.terminal_id, "PTY_ECHO:world")
        resized = runtime.terminals.resize(
            terminal.terminal_id, cols=132, rows=41)
        assert resized["supported"] is True
        assert runtime.terminals.get(terminal.terminal_id).cols == 132
    finally:
        runtime.shutdown()


def test_restart_policy_preserves_one_identity_and_monotonic_output(tmp_path):
    runtime = _runtime(tmp_path)
    marker = tmp_path / "attempt.txt"
    script = (
        "import pathlib,sys\n"
        f"p=pathlib.Path({str(marker)!r})\n"
        "n=int(p.read_text())+1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "print(f'attempt={n}', flush=True)\n"
        "sys.exit(9 if n == 1 else 0)\n"
    )
    try:
        process = runtime.processes.start(
            [sys.executable, "-u", "-c", script], owner=_owner(),
            cwd=str(tmp_path), restart="on_failure", max_attempts=2,
            restart_delay_s=0.02,
        )
        finished = runtime.processes.wait(process.process_id, timeout=10)
        assert finished.state == "exited"
        assert finished.exit_code == 0
        assert finished.attempt == 2
        page = runtime.processes.logs(
            process.process_id, max_bytes=1024 * 1024)
        text = _page_text(page)
        assert "attempt=1" in text
        assert "attempt=2" in text
        types = [event.event_type for event in runtime.events(
            entity_kind="process", entity_id=process.process_id)]
        assert types == [
            "process.dispatch_reserved", "process.started", "process.restart_scheduled",
            "process.restarted", "process.exited",
        ]
    finally:
        runtime.shutdown()


def test_health_wait_is_bounded_and_persists_wake_evidence(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        process = runtime.processes.start(
            [sys.executable, "-u", "-c", "import time; time.sleep(10)"],
            owner=_owner(), cwd=str(tmp_path),
            health_check={"kind": "process"},
        )
        healthy = runtime.processes.wait(
            process.process_id, condition="healthy", timeout=2)
        assert healthy.state == "healthy"
        assert healthy.health["status"] == "healthy"
        stopped = runtime.processes.stop(process.process_id)
        assert stopped.state == "terminated"
    finally:
        runtime.shutdown()


def test_backend_restart_fences_stale_handles_without_duplicate_relaunch(tmp_path):
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    repository = ExecutionRepository(
        str(tmp_path / "execution.sqlite3"), artifact_store=artifacts)
    terminal = repository.create_terminal(
        terminal_id="term-old", owner=_owner(), profile="powershell",
        cwd=str(tmp_path), cols=80, rows=24, transport="conpty",
        capabilities={"true_pty": True, "backend_restart_survival": False},
        pid=111, pid_started_at=22.0, backend_instance_id="backend-old",
    )
    from execution_hosts.models import ProcessRecipe
    process = repository.create_process(
        process_id="proc-old", owner=_owner(),
        recipe=ProcessRecipe(
            argv=(sys.executable, "-c", "print(1)"), cwd=str(tmp_path)),
        pid=222, pid_started_at=33.0, backend_instance_id="backend-old",
    )
    calls: list[tuple[int, float]] = []

    def probe(pid, started_at):
        calls.append((pid, started_at))
        return {"pid": pid, "exists": True, "identity_matches": True}

    terminated: list[tuple[int, float]] = []

    def terminate(pid, started_at):
        terminated.append((pid, started_at))
        return True

    report = repository.reconcile_stale_backends(
        "backend-new", pid_probe=probe, pid_terminator=terminate)
    assert report == {"terminals": [terminal.terminal_id], "processes": [process.process_id]}
    assert calls == [(111, 22.0), (222, 33.0)]
    assert terminated == calls
    terminal_after = repository.get_terminal(terminal.terminal_id)
    process_after = repository.get_process(process.process_id)
    assert terminal_after.state == "unknown_effect"
    assert process_after.state == "unknown_effect"
    assert terminal_after.recovery["status"] == "pid_terminated_on_reconcile"
    assert process_after.recovery["terminated"] is True
    assert terminal_after.recovery["survives_backend_restart"] is False
    assert process_after.recovery["automatic_restart_suppressed"] is True
    assert process_after.recovery["controllable"] is False


def test_failed_reconcile_kill_keeps_exact_live_worktree_owner(
    tmp_path, monkeypatch,
):
    repository = ExecutionRepository(
        str(tmp_path / "execution.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(str(tmp_path / "artifacts")),
    )
    from execution_hosts.models import ProcessRecipe

    process = repository.create_process(
        process_id="proc-ambiguous",
        owner=_owner(),
        recipe=ProcessRecipe(
            argv=(sys.executable, "-c", "print(1)"), cwd=str(tmp_path),
        ),
        pid=222,
        pid_started_at=33.0,
        backend_instance_id="backend-old",
    )
    observation = {"pid": 222, "exists": True, "identity_matches": True}
    repository.reconcile_stale_backends(
        "backend-new",
        pid_probe=lambda _pid, _started: dict(observation),
        pid_terminator=lambda _pid, _started: False,
    )
    assert repository.get_process(process.process_id).state == "unknown_effect"

    monkeypatch.setattr(
        execution_repository, "_probe_pid",
        lambda _pid, _started: dict(observation),
    )
    assert repository.has_live_worktree_owner("worktree-2") is True
    monkeypatch.setattr(
        execution_repository, "_probe_pid",
        lambda _pid, _started: {
            "pid": 222, "exists": False, "identity_matches": False,
        },
    )
    assert repository.has_live_worktree_owner("worktree-2") is False


def test_live_process_record_without_runtime_is_fenced_unknown(tmp_path):
    runtime = _runtime(tmp_path, reconcile=False)
    from execution_hosts.models import ProcessRecipe

    process = runtime.repository.create_process(
        process_id="proc-missing-runtime",
        owner=_owner(),
        recipe=ProcessRecipe(
            argv=(sys.executable, "-c", "print(1)"), cwd=str(tmp_path),
        ),
        pid=222,
        pid_started_at=33.0,
        backend_instance_id=runtime.backend_instance_id,
    )

    with pytest.raises(ExecutionUnavailable, match="without a controllable runtime"):
        runtime.processes.stop(process.process_id)
    fenced = runtime.repository.get_process(process.process_id)
    assert fenced.state == "unknown_effect"
    assert fenced.recovery["status"] == "live_runtime_missing"


def test_cwd_and_string_command_contracts_prevent_implicit_rebinding(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        with pytest.raises(ExecutionValidationError, match="cwd is required"):
            runtime.processes.start(
                [sys.executable, "-c", "print(1)"], owner=_owner(), cwd="")
        with pytest.raises(ExecutionValidationError, match="string commands require"):
            runtime.processes.start(
                "echo ambiguous", owner=_owner(), cwd=str(tmp_path))
        missing = tmp_path / "missing"
        with pytest.raises(ExecutionValidationError, match="not a directory"):
            runtime.terminals.open(
                owner=_owner(), cwd=str(missing), profile="powershell")
        with pytest.raises(ExecutionNotFound):
            runtime.terminals.attach("not-real")
    finally:
        runtime.shutdown()
