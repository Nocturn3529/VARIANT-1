"""Contracts for the single execution core behind ``run_command``."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from capability_broker import CapabilityBroker, InvocationContext
from execution_hosts import create_execution_runtime
from execution_hosts.capabilities import register_execution_tools
from run_context import Variant1RunContext, bind_run_context
from session_runtime import RuntimeIdentity
import shell_tool
import tools
from work_fabric.scope import WorkScope


_RUNTIME_DEPS = {}


@pytest.fixture(autouse=True)
def shell_tool_deps():
    deps = shell_tool.ShellToolDeps(
        execution=object(),
        host=SimpleNamespace(),
    )
    with shell_tool.bind_shell_tool_deps(deps):
        yield deps


@pytest.fixture
def workspace(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "hello.txt").write_text("hi", encoding="utf-8")
    ctx = Variant1RunContext.create(
        source="chat",
        work_scope=WorkScope(chat_id="chat-shell", branch_id="branch-shell"),
        metadata={"working_directory": str(root)},
    )
    with bind_run_context(ctx):
        yield root


@pytest.fixture
def execution_runtime(tmp_path: Path, shell_tool_deps):
    runtime = create_execution_runtime(data_dir=str(tmp_path / "execution-runtime"))
    host = SimpleNamespace(remote_handle_routers={})
    deps = shell_tool.ShellToolDeps(
        execution=runtime,
        host=host,
    )
    _RUNTIME_DEPS[id(runtime)] = deps
    try:
        with shell_tool.bind_shell_tool_deps(deps):
            yield runtime
    finally:
        _RUNTIME_DEPS.pop(id(runtime), None)
        runtime.shutdown()


def test_resolve_cwd_falls_back_to_process_directory_when_no_project():
    assert shell_tool.resolve_cwd(None) == os.path.realpath(os.getcwd())


def test_resolve_cwd_defaults_to_current_project(workspace: Path):
    assert shell_tool.resolve_cwd(None) == os.path.realpath(str(workspace))


def test_resolve_cwd_strips_redundant_root_folder_name(workspace: Path):
    child = workspace / "out"
    child.mkdir()
    assert shell_tool.resolve_cwd(f"{workspace.name}{os.sep}out") == os.path.realpath(
        str(child)
    )
    assert shell_tool.resolve_cwd("out") == os.path.realpath(str(child))


def test_resolve_cwd_keeps_same_user_full_filesystem_access(workspace: Path):
    outside = workspace.parent / "outside"
    outside.mkdir()
    assert shell_tool.resolve_cwd(str(outside)) == os.path.realpath(str(outside))


def test_project_environment_is_default_for_new_processes(
    tmp_path: Path,
):
    root = tmp_path / "project-env"
    root.mkdir()
    context = Variant1RunContext.create(
        source="chat",
        work_scope=WorkScope(chat_id="chat-env"),
        metadata={
            "working_directory": str(root),
            "project_environment": {
            "profile_id": "project-env",
            "variables": {"VARIANT1_WORKSPACE_TEST": "from-workspace"},
            "shell": {"profile": "powershell" if os.name == "nt" else "sh"},
            },
        },
    )
    deps = shell_tool.ShellToolDeps(
        execution=object(), host=SimpleNamespace()
    )
    with shell_tool.bind_shell_tool_deps(deps), bind_run_context(context):
        environment, profile_id, terminal_profile = shell_tool._project_environment({})
    assert environment["VARIANT1_WORKSPACE_TEST"] == "from-workspace"
    assert profile_id == "project-env"
    assert terminal_profile in {"powershell", "sh"}


@pytest.mark.asyncio
async def test_one_shot_command_runs_through_execution_runtime(
    workspace: Path, execution_runtime,
):
    result = await shell_tool.run_command({
        "argv": [
            sys.executable, "-c", "print(open('hello.txt').read())",
        ],
        "cwd": str(workspace),
    })

    assert "hi" in result
    assert "exit: 0" in result
    assert "process_id: proc_" in result
    records = execution_runtime.processes.list(scope=WorkScope(
        chat_id="chat-shell", branch_id="branch-shell",
    ))
    assert len(records) == 1
    assert records[0].state == "exited"


@pytest.mark.asyncio
async def test_one_shot_combines_program_command_with_argument_argv(
    workspace: Path, execution_runtime,
):
    result = await shell_tool.run_command({
        "command": sys.executable,
        "argv": ["-c", "print('COMBINED_ARGV_OK')"],
        "cwd": str(workspace),
    })

    assert "COMBINED_ARGV_OK" in result
    assert "shell: exact argv" in result


@pytest.mark.asyncio
async def test_command_inherits_ambient_environment(
    workspace: Path, execution_runtime, monkeypatch,
):
    monkeypatch.setenv("VARIANT1_TEST_VARIABLE", "forwarded")
    result = await shell_tool.run_command({
        "argv": [
            sys.executable, "-c",
            "import os; print(os.environ['VARIANT1_TEST_VARIABLE'])",
        ],
        "cwd": str(workspace),
    })
    assert "forwarded" in result


@pytest.mark.asyncio
async def test_command_can_read_outside_current_project(
    workspace: Path, execution_runtime,
):
    outside = workspace.parent / "outside.txt"
    outside.write_text("reachable", encoding="utf-8")
    command = (
        f"Get-Content '{outside}'"
        if os.name == "nt"
        else f"cat {shlex.quote(str(outside))}"
    )
    result = await shell_tool.run_command({
        "command": command, "cwd": str(workspace),
    })
    assert "reachable" in result


@pytest.mark.asyncio
async def test_timeout_stops_the_owned_process_tree(
    workspace: Path, execution_runtime,
):
    marker = workspace / "orphan.txt"
    (workspace / "child.py").write_text(
        "import pathlib,time\ntime.sleep(2.5)\npathlib.Path('orphan.txt').write_text('alive')\n",
        encoding="utf-8",
    )
    (workspace / "parent.py").write_text(
        "import subprocess,sys,time\nsubprocess.Popen([sys.executable,'child.py'])\ntime.sleep(30)\n",
        encoding="utf-8",
    )

    with pytest.raises(tools.ToolError, match="timed out"):
        await shell_tool.run_command({
            "argv": [sys.executable, "parent.py"],
            "cwd": str(workspace),
            "timeout": 1,
        })
    await asyncio.sleep(3)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_cancellation_stops_descendants_before_propagating(
    workspace: Path, execution_runtime,
):
    marker = workspace / "cancelled-orphan.txt"
    started = workspace / "parent-started.txt"
    (workspace / "cancel-child.py").write_text(
        "import pathlib,time\ntime.sleep(2)\npathlib.Path('cancelled-orphan.txt').write_text('alive')\n",
        encoding="utf-8",
    )
    (workspace / "cancel-parent.py").write_text(
        "import pathlib,subprocess,sys,time\nsubprocess.Popen([sys.executable,'cancel-child.py'])\npathlib.Path('parent-started.txt').write_text('ready')\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    task = asyncio.create_task(shell_tool.run_command({
        "argv": [sys.executable, "cancel-parent.py"],
        "cwd": str(workspace),
        "timeout": 30,
    }))
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.05)
    assert started.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.5)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_bypass_tree_cleanup(
    workspace: Path, execution_runtime, monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    original = execution_runtime.processes.stop

    def delayed_stop(process_id, *, force=True):
        entered.set()
        assert release.wait(timeout=5)
        return original(process_id, force=force)

    monkeypatch.setattr(execution_runtime.processes, "stop", delayed_stop)
    task = asyncio.create_task(shell_tool.run_command({
        "argv": [
            sys.executable, "-c", "import time; time.sleep(30)",
        ],
        "cwd": str(workspace),
    }))
    for _ in range(100):
        if any(record.state == "running" and record.pid
               for record in execution_runtime.processes.list()):
            break
        await asyncio.sleep(0.02)
    assert any(record.state == "running" and record.pid
               for record in execution_runtime.processes.list())
    task.cancel()
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.02)
    assert entered.is_set()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_nonzero_exit_is_result_unless_check_is_true(
    workspace: Path, execution_runtime,
):
    command = "Write-Error 'boom'" if os.name == "nt" else "echo boom >&2; exit 7"
    result = await shell_tool.run_command({
        "command": command, "cwd": str(workspace),
    })

    assert "boom" in result
    assert "exit: " in result
    with pytest.raises(tools.ToolError, match="boom"):
        await shell_tool.run_command({
            "command": command, "cwd": str(workspace), "check": True,
        })


@pytest.mark.asyncio
async def test_process_mode_starts_and_lists_durable_processes(
    workspace: Path, execution_runtime,
):
    started = json.loads(await shell_tool.run_command({
        "mode": "process",
        "argv": [sys.executable, "-u", "-c", "print('PROCESS_READY')"],
        "cwd": str(workspace),
    }))
    record = execution_runtime.processes.wait(started["id"], timeout=10)
    listed = json.loads(await shell_tool.run_command({"mode": "processes"}))
    logs = execution_runtime.processes.logs(record.process_id).to_dict()

    assert record.exit_code == 0
    assert record.process_id in {item["id"] for item in listed}
    assert "PROCESS_READY" in "".join(
        frame.get("text", "") for frame in logs["frames"]
    )


def _kernel_host(runtime):
    registry = tools.ToolRegistry()
    composed = SimpleNamespace(execution=runtime)
    host = SimpleNamespace(
        require_runtime=lambda: composed,
        remote_handle_routers={},
    )
    shell_tool.register(
        registry,
        shell_tool.ShellToolDeps(
            execution=runtime,
            host=host,
        ),
    )

    async def remote_dispatch(_args):
        raise AssertionError("remote handles route through the host router")

    registry.register(tools.Tool(
        "remote_handle_dispatch",
        "Internal remote handle transport.",
        remote_dispatch,
        hidden=True,
        visibility="broker_only",
    ))
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=SimpleNamespace(
            ensure_runtime=lambda chat_id: SimpleNamespace(
                chat_id=chat_id,
                identity=RuntimeIdentity(
                    catalog_release_id="astb.test.shell.v1",
                    environment_digest="test-shell",
                    mount_revision=1,
                ),
                kernel_generation=1,
            )
        ),
        enabled_resolver=lambda: {"run_command", "remote_handle_dispatch"},
    )
    composed.registry = registry
    composed.broker = broker
    register_execution_tools(host)
    return host, broker


def _kernel_context():
    return InvocationContext(
        chat_id="chat-execution-handle",
        run_id="run-execution-handle",
        outer_tool_call_id="outer-execution-handle",
        cell_execution_id="cell-execution-handle",
        nested_call_id="nested-execution-handle",
        catalog_release_id="astb.test.shell.v1",
        surface="ipython",
        work_scope=WorkScope(
            chat_id="chat-execution-handle", branch_id="branch-execution-handle",
        ),
    )


@pytest.mark.asyncio
async def test_broker_large_listing_limit_keeps_service_cap(execution_runtime, monkeypatch):
    _host, broker = _kernel_host(execution_runtime)
    observed_limits = []

    def bounded_list(*, scope, limit):
        observed_limits.append(limit)
        return []

    monkeypatch.setattr(execution_runtime.processes, "list", bounded_list)
    receipt = await broker.invoke_name(
        "run_command", {"mode": "processes", "limit": 20_000}, _kernel_context(),
    )
    assert receipt.ok, receipt.to_dict()
    assert observed_limits == [200]


@pytest.mark.asyncio
async def test_large_listing_limit_does_not_block_once_command(workspace, execution_runtime):
    _host, broker = _kernel_host(execution_runtime)
    receipt = await broker.invoke_name(
        "run_command",
        {"argv": [sys.executable, "-c", "from pathlib import Path; Path('ran.txt').write_text('once')"],
         "cwd": str(workspace), "limit": 20_000},
        _kernel_context(),
    )
    assert receipt.ok, receipt.to_dict()
    assert (workspace / "ran.txt").read_text(encoding="utf-8") == "once"


@pytest.mark.asyncio
async def test_terminal_mode_returns_one_fluent_ipython_handle(
    workspace: Path, execution_runtime,
):
    host, broker = _kernel_host(execution_runtime)
    source = (
        "import sys,time\n"
        "print('TERMINAL_READY', flush=True)\n"
        "for line in sys.stdin:\n"
        " print('TERMINAL_ECHO:' + line.rstrip(), flush=True)\n"
    )
    context = _kernel_context()
    receipt = await broker.invoke_name(
        "run_command",
        {
            "mode": "terminal",
            "argv": [sys.executable, "-u", "-c", source],
            "cwd": str(workspace),
            "force_pipe_fallback": True,
        },
        context,
    )
    assert receipt.ok, receipt.to_dict()
    handle = receipt.result_value["$variant1_handle"]
    router = host.remote_handle_routers["execution"]
    written = await router(context, handle, "write", {"data": "hello\n"})
    handle = written["$variant1_handle"]
    deadline = time.monotonic() + 5
    text = ""
    while time.monotonic() < deadline:
        page = await router(context, handle, "read", {"after_cursor": 0})
        text = "".join(frame.get("text", "") for frame in page["frames"])
        if "TERMINAL_ECHO:hello" in text:
            break
        await asyncio.sleep(0.03)
    closed = await router(context, handle, "close", {})

    assert handle["service"] == "execution"
    assert handle["kind"] == "terminal"
    assert {row["name"] for row in handle["methods"]["items"]} == {
        "refresh", "inspect", "read", "write", "resize", "signal",
        "wait", "detach", "close",
    }
    assert "TERMINAL_READY" in text
    assert "TERMINAL_ECHO:hello" in text
    assert closed["$variant1_handle"]["metadata"]["state"] in {
        "exited", "terminated",
    }


@pytest.mark.asyncio
async def test_one_shot_kernel_result_carries_reconnectable_process_handle(
    workspace: Path, execution_runtime,
):
    host, broker = _kernel_host(execution_runtime)
    receipt = await broker.invoke_name(
        "run_command",
        {"command": "Write-Output 42" if os.name == "nt" else "printf 42",
         "cwd": str(workspace)},
        _kernel_context(),
    )

    assert receipt.ok, receipt.to_dict()
    result = receipt.result_value
    assert result["schema"] == "variant1.command-result.v1"
    assert result["ok"] is True
    assert "42" in result["text"]
    assert result["stdout"].strip() == "42"
    assert result["stderr"] == ""
    assert result["exit_code"] == 0
    assert result["truncated"] is False
    assert result["process"]["$variant1_handle"]["kind"] == "process"


@pytest.mark.asyncio
async def test_one_shot_kernel_nonzero_result_preserves_diagnostics(
    workspace: Path, execution_runtime,
):
    _host, broker = _kernel_host(execution_runtime)
    receipt = await broker.invoke_name(
        "run_command",
        {
            "argv": [
                sys.executable,
                "-c",
                "import sys; print('warming', file=sys.stderr); raise SystemExit(75)",
            ],
            "cwd": str(workspace),
        },
        _kernel_context(),
    )

    assert receipt.ok, receipt.to_dict()
    result = receipt.result_value
    assert result["ok"] is False
    assert result["exit_code"] == 75
    assert "warming" in result["stderr"]


@pytest.mark.asyncio
async def test_stale_process_handle_can_wait_but_cannot_signal(
    workspace: Path, execution_runtime,
):
    host, broker = _kernel_host(execution_runtime)
    context = _kernel_context()
    receipt = await broker.invoke_name(
        "run_command",
        {
            "mode": "process",
            "argv": [sys.executable, "-u", "-c", "print('finished')"],
            "cwd": str(workspace),
        },
        context,
    )
    assert receipt.ok, receipt.to_dict()
    stale = receipt.result_value["$variant1_handle"]
    finished = execution_runtime.processes.wait(stale["id"], timeout=10)
    assert finished.revision > int(stale["revision"])

    router = host.remote_handle_routers["execution"]
    waited = await router(context, stale, "wait", {"timeout": 1})

    assert waited["$variant1_handle"]["revision"] == finished.revision
    assert waited["$variant1_handle"]["metadata"]["state"] == "exited"
    with pytest.raises(tools.ToolError, match="stale execution.process handle") as failure:
        await router(context, stale, "signal", {"name": "interrupt"})
    assert failure.value.code == 'stale_execution_handle'
    assert 'replacement = handle.refresh()' in str(failure.value)
    replacement = await router(context, stale, 'refresh', {})
    assert replacement['$variant1_handle']['revision'] == finished.revision
    assert stale['revision'] < finished.revision


@pytest.mark.asyncio
async def test_stale_terminal_handle_can_wait_but_cannot_write(
    workspace: Path, execution_runtime,
):
    host, broker = _kernel_host(execution_runtime)
    context = _kernel_context()
    receipt = await broker.invoke_name(
        "run_command",
        {
            "mode": "terminal",
            "argv": [sys.executable, "-u", "-c", "print('finished')"],
            "cwd": str(workspace),
            "force_pipe_fallback": True,
        },
        context,
    )
    assert receipt.ok, receipt.to_dict()
    stale = receipt.result_value["$variant1_handle"]
    finished = execution_runtime.terminals.wait(stale["id"], timeout=10)
    assert finished.revision > int(stale["revision"])

    router = host.remote_handle_routers["execution"]
    waited = await router(context, stale, "wait", {"timeout": 1})

    assert waited["$variant1_handle"]["revision"] == finished.revision
    assert waited["$variant1_handle"]["metadata"]["state"] == "exited"
    with pytest.raises(tools.ToolError, match="stale execution.terminal handle"):
        await router(context, stale, "write", {"data": "late input"})


def test_run_command_has_small_mode_contract_without_git_or_method_ops(
    shell_tool_deps,
):
    registry = tools.ToolRegistry()
    shell_tool.register(
        registry,
        shell_tool.ShellToolDeps(
            execution=object(),
            host=SimpleNamespace(),
        ),
    )
    tool = registry.get("run_command")

    assert tool is not None
    assert tool.category == "shell"
    assert set(tool.params["mode"]["enum"]) == {
        "once", "terminal", "process", "terminals", "processes",
    }
    assert "op" not in tool.params
    assert not any(name.startswith(("git_", "worktree_", "terminal_", "process_"))
                   for name in tool.params)
    assert "same-user full filesystem access" in tool.description.lower()
    assert "one-shot" in tool.params["argv"]["desc"]


@pytest.mark.asyncio
async def test_run_command_rejects_deleted_op_dispatcher(execution_runtime):
    registry = tools.ToolRegistry()
    shell_tool.register(registry, _RUNTIME_DEPS[id(execution_runtime)])
    with pytest.raises(tools.ToolError, match="unknown argument.*op"):
        await registry.get("run_command").run({
            "op": "git_status", "repository_id": "repo-old",
        })


@pytest.mark.asyncio
async def test_run_command_requires_the_composed_execution_core():
    token = shell_tool._CURRENT_DEPS.set(None)
    try:
        with pytest.raises(tools.ToolError, match="not bound to the HostRuntime"):
            await shell_tool.run_command({"command": "echo never"})
    finally:
        shell_tool._CURRENT_DEPS.reset(token)


def test_shell_seed_contains_no_second_popen_backend():
    source = Path(shell_tool.__file__).read_text(encoding="utf-8")
    assert "subprocess.Popen" not in source
    assert "run_command_sync" not in source
    assert "_run_coding_op" not in source


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
@pytest.mark.asyncio
async def test_ordinary_git_is_composed_as_a_shell_command(
    workspace: Path, execution_runtime,
):
    result = await shell_tool.run_command({
        "command": "git init && git status --short"
        if os.name != "nt"
        else "git init; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; git status --short",
        "cwd": str(workspace),
    })
    assert "exit: 0" in result
