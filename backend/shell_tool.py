"""Full OS shell tool — current-project command execution.

``run_command`` executes PowerShell on Windows and ``/bin/sh`` elsewhere, then
returns stdout, stderr, and the exit code. Runtime behavior:

  * **Project-targeted** — the current project root is the default cwd,
    while explicit working directories and command paths retain same-user full
    filesystem access.
  * Process tree kill on timeout; output capped.

This is not an OS sandbox. Commands run with the backend process's authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

import tools
from run_context import current_run_context


@dataclass(frozen=True)
class ShellToolDeps:
    execution: object
    host: object


_CURRENT_DEPS: ContextVar[ShellToolDeps | None] = ContextVar(
    "variant1_shell_tool_deps", default=None
)


def _bound_deps() -> ShellToolDeps:
    deps = _CURRENT_DEPS.get()
    if deps is None:
        raise tools.ToolError("run_command is not bound to the HostRuntime")
    return deps


@contextmanager
def bind_shell_tool_deps(deps: ShellToolDeps):
    if not isinstance(deps, ShellToolDeps):
        raise TypeError("ShellToolDeps is required")
    token = _CURRENT_DEPS.set(deps)
    try:
        yield
    finally:
        _CURRENT_DEPS.reset(token)


_EXECUTION_MODES = frozenset({
    "once", "terminal", "process", "terminals", "processes",
})

_RUN_COMMAND_PARAMS = {
    "command": {
        "type": "string", "required": False,
        "desc": "PowerShell script on Windows or /bin/sh command elsewhere. Required for mode=once unless argv is supplied.",
    },
    "mode": {
        "type": "string", "required": False,
        "default": "once",
        "enum": sorted(_EXECUTION_MODES),
        "desc": "once waits for command exit and returns a result with its process handle; process starts an owned app/service; terminal starts an interactive handle; terminals/processes reconnect. Owned children can outlive their launcher.",
    },
    "cwd": {
        "type": "string", "required": False,
        "desc": "Working directory. Relative paths start from the current project root.",
    },
    "timeout": {
        "type": "number", "required": False,
        "default": 120,
        "minimum": 0, "maximum": 600,
        "desc": "Seconds before a one-shot command is terminated.",
    },
    "check": {
        "type": "boolean", "required": False,
        "default": False,
        "desc": (
            "For mode=once, raise on an ordinary nonzero exit when true. "
            "Default false returns stdout, stderr, and exit_code for recovery."
        ),
    },
    "argv": {
        "type": "array", "required": False, "items": {"type": "string"},
        "desc": (
            "Exact argv for one-shot, terminal, or process mode. Use instead of "
            "command, or pass command as the program and argv as its arguments."
        ),
    },
    "environment": {"type": "object", "required": False},
    "environment_profile_id": {"type": "string", "required": False},
    "profile": {"type": "string", "required": False},
    "cols": {"type": "integer", "required": False, "minimum": 2, "maximum": 1000},
    "rows": {"type": "integer", "required": False, "minimum": 1, "maximum": 500},
    "restart": {
        "type": "string", "required": False,
        "default": "never",
        "enum": ["never", "on_failure", "always"],
    },
    "max_attempts": {"type": "integer", "required": False, "default": 1, "minimum": 1, "maximum": 100},
    "restart_delay_s": {"type": "number", "required": False, "default": 0.25, "minimum": 0, "maximum": 60},
    "health_check": {"type": "object", "required": False},
    "force_pipe_fallback": {"type": "boolean", "required": False},
    "limit": {
        "type": "integer", "required": False, "minimum": 1,
        "desc": "Requested maximum records for mode=processes or mode=terminals (default 50, capped at 200). This is a listing count, not a command-output character limit.",
    },
    "idempotency_key": {"type": "string", "required": False},
}

MAX_OUTPUT_CHARS = 100_000
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600

def shell_cwd_candidates() -> list[str]:
    """Return preferred cwd candidates, current project first.

    These paths choose where a command starts; they are not an access-control
    boundary. ``run_command`` executes with the same filesystem authority as the
    VARIANT-1 backend process.
    """
    roots: list[str] = []

    deps = _bound_deps()
    try:
        from project_context import current_project_context
        active = current_project_context().cwd
    except Exception:
        active = ""
    if not active:
        try:
            ctx = current_run_context()
            active = str(
                ((ctx.metadata if ctx else {}) or {}).get("working_directory") or ""
            ).strip()
        except Exception:
            active = ""
    if active:
        roots.append(active)

    roots.append(os.path.expanduser("~"))
    try:
        roots.append(os.getcwd())
    except OSError:
        pass

    out = []
    seen = set()
    for r in roots:
        try:
            rp = os.path.realpath(os.path.abspath(os.path.expanduser(str(r))))
        except OSError:
            continue
        if not os.path.isdir(rp):
            continue
        key = rp.lower() if os.name == "nt" else rp
        if key in seen:
            continue
        seen.add(key)
        out.append(rp)
    return out


def _strip_redundant_root_name(rel: str, roots: list[str]) -> str:
    """Drop a leading folder name that already is the project root basename.

    When the project root is ``C:\\Users\\…\\SCRATCH`` and the model passes
    ``cwd=SCRATCH\\out`` (common when the user names the folder SCRATCH), a
    naive join becomes ``…\\SCRATCH\\SCRATCH\\out``. Strip one matching
    leading segment so relative paths stay under the selected directory.
    """
    cleaned = str(rel or "").strip().replace("/", os.sep)
    while cleaned.startswith("." + os.sep):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("\\/") if os.name == "nt" else cleaned.lstrip("/")
    if not cleaned:
        return cleaned
    parts = cleaned.split(os.sep)
    first = parts[0]
    if not first:
        return cleaned
    for root in roots:
        base = os.path.basename(str(root).rstrip("\\/"))
        if not base:
            continue
        if first.lower() == base.lower():
            return os.sep.join(parts[1:])
    return cleaned


def resolve_cwd(cwd: str | None) -> str:
    """Resolve command cwd from the current project context."""
    roots = shell_cwd_candidates()
    if not roots:
        raise tools.ToolError("run_command could not find an existing working directory")

    if cwd and str(cwd).strip():
        candidate = os.path.expandvars(os.path.expanduser(str(cwd).strip()))
        if not os.path.isabs(candidate):
            # Relative to the current project (or ordinary fallback directory).
            rel = _strip_redundant_root_name(candidate, roots)
            candidate = roots[0] if not rel else os.path.join(roots[0], rel)
        candidate = os.path.realpath(os.path.abspath(candidate))
    else:
        candidate = roots[0]

    if not os.path.isdir(candidate):
        raise tools.ToolError(
            f"cwd is not a directory: {candidate}. "
            f"Relative cwd starts from {roots[0]} "
            f"(example: out — not {os.path.basename(roots[0])}{os.sep}out).")
    return candidate


def _timeout_s(raw) -> int:
    try:
        n = int(raw if raw is not None else DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        n = DEFAULT_TIMEOUT_S
    return max(1, min(MAX_TIMEOUT_S, n))


def _clip_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    s = text if isinstance(text, str) else str(text or "")
    if len(s) <= limit:
        return s, False
    head = limit // 2
    tail = limit // 4
    return s[:head] + "\n… output truncated …\n" + s[-tail:], True


def _execution_invocation():
    try:
        from capability_broker import current_capability_invocation

        return current_capability_invocation()
    except Exception:
        return None


def _execution_scope(invocation=None):
    from work_fabric.scope import WorkScope, coerce_work_scope

    if invocation is not None:
        scope = coerce_work_scope(getattr(invocation, "work_scope", None))
        chat_id = str(getattr(invocation, "chat_id", "") or "")
        if chat_id and not scope.chat_id:
            scope = scope.with_updates(chat_id=chat_id)
        return scope
    context = current_run_context()
    if context is not None:
        return coerce_work_scope(getattr(context, "work_scope", None))
    return WorkScope()


def _execution_owner(scope):
    from execution_hosts import ExecutionOwner

    if scope.goal_id:
        return ExecutionOwner("goal", scope.goal_id, scope)
    if scope.workspace_id:
        return ExecutionOwner("workspace", scope.workspace_id, scope)
    if scope.chat_id:
        return ExecutionOwner("chat", scope.chat_id, scope)
    context = current_run_context()
    run_id = str(getattr(context, "run_id", "") or "").strip()
    return ExecutionOwner(
        "run", run_id or f"run-command-{uuid.uuid4().hex}", scope
    )


def _project_environment(args: dict) -> tuple[dict[str, str], str, str]:
    variables: dict[str, str] = {}
    profile_id = ""
    terminal_profile = ""
    try:
        from project_context import current_project_context
        profile = dict(current_project_context().environment)
        variables.update({
            str(key): str(value)
            for key, value in dict(profile.get("variables") or {}).items()
        })
        profile_id = str(profile.get("profile_id") or "")
        terminal_profile = str(
            dict(profile.get("shell") or {}).get("profile") or ""
        )
    except Exception:
        pass
    variables.update({
        str(key): str(value)
        for key, value in dict(args.get("environment") or {}).items()
    })
    return (
        variables,
        str(args.get("environment_profile_id") or profile_id),
        str(args.get("profile") or terminal_profile),
    )


def _execution_runtime():
    return _bound_deps().execution


def _execution_handle(record, invocation):
    if invocation is not None:
        from execution_hosts.capabilities import execution_handle_envelope

        return execution_handle_envelope(_bound_deps().host, invocation, record)
    return record.to_dict()


def _execution_seed_result(value, invocation):
    # Inside the Python worker the bridge must receive typed dictionaries so it can
    # reconstruct terminal/process handles. Direct tool calls receive readable
    # JSON, matching the browser seeds' host-versus-kernel result behavior.
    if invocation is not None:
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _shell_argv(command: str) -> tuple[tuple[str, ...], str]:
    cmd = str(command or "").strip()
    if not cmd:
        raise tools.ToolError("run_command needs a non-empty 'command' string")
    if platform.system() == "Windows":
        script = (
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
            "$OutputEncoding=[System.Text.UTF8Encoding]::new(); "
            "$ErrorActionPreference='Stop'; "
            + cmd
        )
        return (
            (
                "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                "-Command", script,
            ),
            "PowerShell",
        )
    return (("/bin/sh", "-lc", cmd), "/bin/sh")


def _exact_argv(args: dict, *, command_required: bool = True) -> tuple[str, ...] | None:
    raw = args.get("argv")
    if isinstance(raw, list) and raw:
        return tuple(str(item) for item in raw)
    command = str(args.get("command") or "").strip()
    if command:
        return _shell_argv(command)[0]
    if command_required:
        raise tools.ToolError("run_command needs 'command' or 'argv'")
    return None


def _execution_process_id(kind: str, args: dict, invocation, owner) -> str:
    key = str(
        args.get("idempotency_key")
        or getattr(invocation, "idempotency_key", "")
        or ""
    ).strip()
    if not key:
        return ""
    digest = hashlib.sha256(
        f"{kind}\0{owner.kind}\0{owner.owner_id}\0{key}".encode("utf-8")
    ).hexdigest()
    return ("term_" if kind == "terminal" else "proc_") + digest


async def _run_execution_mode(mode: str, args: dict):
    """Construct or list fluent execution handles through one runtime."""

    runtime = _execution_runtime()
    invocation = _execution_invocation()
    scope = _execution_scope(invocation)
    owner = _execution_owner(scope)
    limit = max(1, min(int(args.get("limit") or 50), 200))

    if mode == "terminals":
        records = runtime.terminals.list(scope=scope, limit=limit)
        return _execution_seed_result(
            [_execution_handle(record, invocation) for record in records],
            invocation,
        )
    if mode == "processes":
        records = runtime.processes.list(scope=scope, limit=limit)
        return _execution_seed_result(
            [_execution_handle(record, invocation) for record in records],
            invocation,
        )

    if mode == "terminal":
        environment, _environment_profile_id, terminal_profile = (
            _project_environment(args)
        )
        argv = _exact_argv(args, command_required=False)
        command = str(args.get("command") or "").strip()
        # A command without explicit argv is written into the selected
        # interactive profile so the shell remains available afterward.
        launch_argv = argv if args.get("argv") else None
        record = await runtime.open_terminal(
            owner=owner,
            cwd=resolve_cwd(args.get("cwd")),
            profile=terminal_profile,
            cols=int(args.get("cols") or 120),
            rows=int(args.get("rows") or 30),
            environment=environment,
            argv=launch_argv,
            terminal_id=_execution_process_id(
                "terminal", args, invocation, owner,
            ),
            force_pipe_fallback=bool(args.get("force_pipe_fallback")),
        )
        if command and launch_argv is None:
            runtime.terminals.write(
                record.terminal_id,
                command + ("\r\n" if os.name == "nt" else "\n"),
            )
            record = runtime.terminals.get(record.terminal_id, scope=scope)
        return _execution_seed_result(
            _execution_handle(record, invocation), invocation,
        )

    if mode == "process":
        environment, environment_profile_id, _terminal_profile = (
            _project_environment(args)
        )
        record = await runtime.start_process(
            _exact_argv(args),
            owner=owner,
            cwd=resolve_cwd(args.get("cwd")),
            environment=environment,
            environment_profile_id=environment_profile_id,
            shell=False,
            restart=str(args.get("restart") or "never"),
            max_attempts=int(args.get("max_attempts") or 1),
            restart_delay_s=float(
                0.25 if args.get("restart_delay_s") is None
                else args.get("restart_delay_s")
            ),
            health_check=dict(args.get("health_check") or {}),
            process_id=_execution_process_id(
                "process", args, invocation, owner,
            ),
        )
        return _execution_seed_result(
            _execution_handle(record, invocation), invocation,
        )

    raise tools.ToolError(f"unsupported run_command mode: {mode}")


async def _run_once(args: dict):
    runtime = _execution_runtime()
    invocation = _execution_invocation()
    scope = _execution_scope(invocation)
    owner = _execution_owner(scope)
    raw_argv = args.get("argv")
    if isinstance(raw_argv, list) and raw_argv:
        argv = tuple(str(item) for item in raw_argv)
        command = " ".join(repr(item) for item in argv)
        shell_name = "exact argv"
    else:
        command = str(args.get("command") or "").strip()
        argv, shell_name = _shell_argv(command)
    workdir = resolve_cwd(args.get("cwd"))
    timeout_s = _timeout_s(args.get("timeout"))
    environment, environment_profile_id, _terminal_profile = (
        _project_environment(args)
    )
    result = await runtime.run_bounded_process(
        argv,
        owner=owner,
        cwd=workdir,
        environment=environment,
        environment_profile_id=environment_profile_id,
        timeout=timeout_s,
        max_output_bytes=MAX_OUTPUT_CHARS * 6,
        process_id=_execution_process_id(
            "process", args, invocation, owner,
        ),
    )
    record = result.process
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    artifact_refs = list(result.artifact_refs)
    output_cursor = result.output_cursor
    stdout, trunc_out = _clip_output(stdout)
    stderr, trunc_err = _clip_output(stderr, MAX_OUTPUT_CHARS // 2)
    duration = round(result.duration_ms / 1000.0, 3)
    lines = [
        f"$ {command}",
        f"shell: {shell_name}",
        f"cwd: {workdir}",
        f"process_id: {record.process_id}",
        f"exit: {record.exit_code if record.exit_code is not None else record.state}",
        f"duration_s: {duration}",
        f"output_cursor: {output_cursor}",
    ]
    if result.timed_out:
        lines.append(f"(timed out after {timeout_s}s; process tree killed)")
    if result.truncated or trunc_out or trunc_err:
        lines.append("(output truncated)")
    if artifact_refs:
        lines.append("artifact_segments: " + ", ".join(artifact_refs))
    if stdout.strip():
        lines.extend(("--- stdout ---", stdout.rstrip()))
    if stderr.strip():
        lines.extend(("--- stderr ---", stderr.rstrip()))
    if not stdout.strip() and not stderr.strip() and not artifact_refs:
        lines.append("(no output)")
    descendants_active = record.live and record.health.get('leader_alive') is False
    if descendants_active:
        lines.append(
            "The command leader exited; owned descendants remain active. "
            "Use the returned process handle to inspect, wait for, or stop them."
        )
    text = "\n".join(lines)
    if result.timed_out or record.state in {"unknown_effect", "failed"}:
        raise tools.ToolError(text)
    if record.exit_code not in (0, None) and bool(args.get("check", False)):
        raise tools.ToolError(text)
    if invocation is not None:
        return {
            "schema": "variant1.command-result.v1",
            "ok": record.exit_code in (0, None),
            "text": text,
            # Keep the structured kernel contract aligned with the public
            # capability description.  Models should not have to parse the
            # human-readable transcript to recover ordinary process output.
            "stdout": stdout,
            "stderr": stderr,
            "process": _execution_handle(record, invocation),
            "exit_code": record.exit_code,
            "duration_s": duration,
            "owned_descendants_running": bool(descendants_active),
            "output_cursor": output_cursor,
            "artifact_refs": artifact_refs,
            "truncated": bool(result.truncated or trunc_out or trunc_err),
        }
    return text


async def run_command(args: dict):
    payload = tools.validate_arguments(
        "run_command", dict(args or {}), _RUN_COMMAND_PARAMS,
    )
    command = str(payload.get("command") or "").strip()
    argv = payload.get("argv")
    if command and isinstance(argv, list) and argv:
        # Accept the common subprocess-like spelling
        # run_command(command="git", argv=["diff", ...]).  The exact argv
        # remains unambiguous and bypasses shell parsing.
        payload["argv"] = [command, *(str(item) for item in argv)]
        payload.pop("command", None)
    mode = str(payload.get("mode") or "once").strip().casefold()
    if mode not in _EXECUTION_MODES:
        raise tools.ToolError(f"unsupported run_command mode: {mode}")
    if mode != "once" and bool(payload.get("check", False)):
        raise tools.ToolError("run_command check is valid only for mode=once")
    if mode == "once":
        return await _run_once(payload)
    return await _run_execution_mode(mode, payload)


def register(registry, deps: ShellToolDeps) -> None:
    """Register run_command with immutable, call-scoped runtime dependencies."""
    if not isinstance(deps, ShellToolDeps):
        raise TypeError("ShellToolDeps is required")
    windows = platform.system() == "Windows"
    if windows:
        description = (
            "Run a PowerShell command with same-user full filesystem access and "
            "return stdout, stderr, and exit code. The selected chat project "
            "is the default cwd."
        )
        command_desc = "PowerShell script to execute."
    else:
        description = (
            "Run a /bin/sh command with same-user full filesystem access and "
            "return stdout, stderr, and exit code. The selected chat project "
            "is the default cwd."
        )
        command_desc = "/bin/sh command to execute."
    description += (
        " Ordinary nonzero exits are structured results; set check=true only "
        "when raising is intentionally useful."
        " The default mode=once waits for command exit and applies the command timeout. "
        "Its result includes a process handle; children deliberately launched "
        "in the background remain owned and controllable after the command exits. "
        "For applications/services that should remain running, use mode=process "
        "and set cwd to their working directory. "
        "Set mode=terminal or mode=process to receive a fluent durable handle; "
        "use mode=terminals or mode=processes to reconnect. Process wait(exit) "
        "waits for the owned tree, including children after launcher exit. Continued reads, "
        "input, signals, waits, and cleanup live on returned handles. Ordinary "
        "Git is composed with shell commands; review/workspace own higher-level "
        "durable semantics."
    )
    params = {
        **_RUN_COMMAND_PARAMS,
        "command": {**_RUN_COMMAND_PARAMS["command"], "desc": command_desc},
        "timeout": {
            **_RUN_COMMAND_PARAMS["timeout"],
            "maximum": MAX_TIMEOUT_S,
            "desc": f"Seconds before termination (default {DEFAULT_TIMEOUT_S}, "
                    f"max {MAX_TIMEOUT_S}).",
        },
    }
    async def bound_run_command(args):
        with bind_shell_tool_deps(deps):
            return await run_command(args)

    registry.register(tools.Tool(
        "run_command",
        description,
        bound_run_command,
        category="shell",
        params=params,
    ))
