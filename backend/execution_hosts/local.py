"""Local process, pipe-terminal, and POSIX PTY adapters."""

from __future__ import annotations

from dataclasses import dataclass
import os
import signal as signal_module
import subprocess
import threading
import time
from typing import Callable, Sequence

from process_tree import (
    OwnedProcessTree,
    CREATE_SUSPENDED,
    attach_process,
    resume_owned_process,
    dispose_process_tree,
    process_started_at,
)
from .windows_conpty import WindowsConPtyProcess, conpty_available
from .pipe_reader import PipeInput, PipeReader, join_readers, stop_readers


OutputCallback = Callable[[str, bytes], None]
_DEFAULT_CHILD_OUTPUT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BoundedChildResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    cancelled: bool = False
    stdout_limit_exceeded: bool = False
    stderr_limit_exceeded: bool = False

    @property
    def output_limit_exceeded(self) -> bool:
        return self.stdout_limit_exceeded or self.stderr_limit_exceeded


def inherited_environment(delta: dict[str, str] | None = None) -> dict[str, str]:
    result = dict(os.environ)
    result.setdefault("PYTHONIOENCODING", "utf-8")
    for key, value in dict(delta or {}).items():
        result[str(key)] = str(value)
    return result


def terminate_process_tree(
    process: subprocess.Popen,
    *,
    owner: OwnedProcessTree | None = None,
    force: bool = True,
) -> bool:
    descendants = []
    try:
        import psutil

        descendants = psutil.Process(int(process.pid)).children(recursive=True)
    except Exception:
        descendants = []

    def settle_known_descendants() -> None:
        for child in reversed(descendants):
            try:
                child.kill() if force else child.terminate()
            except Exception:
                pass
        if descendants:
            try:
                import psutil

                psutil.wait_procs(descendants, timeout=3)
            except Exception:
                pass

    if process.poll() is not None:
        settle_known_descendants()
        if owner is not None:
            dispose_process_tree(owner, terminate=False)
        return True
    if owner is not None:
        try:
            dispose_process_tree(owner, terminate=True)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            settle_known_descendants()
            if process.poll() is not None:
                return True
        except Exception:
            # The platform fallback below is the final cleanup path.
            pass
    if os.name == "nt":
        try:
            command = ["taskkill.exe", "/PID", str(process.pid), "/T"]
            if force:
                command.append("/F")
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode == 0:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                settle_known_descendants()
                if process.poll() is not None:
                    return True
        except Exception:
            pass
    else:
        try:
            os.killpg(process.pid, signal_module.SIGTERM)
            try:
                process.wait(timeout=2)
                settle_known_descendants()
                return True
            except subprocess.TimeoutExpired:
                if force:
                    os.killpg(process.pid, signal_module.SIGKILL)
                    process.wait(timeout=2)
                    settle_known_descendants()
                    return True
        except Exception:
            pass
    try:
        process.kill() if force else process.terminate()
        process.wait(timeout=2)
    except Exception:
        pass
    settle_known_descendants()
    return process.poll() is not None


class PipeTerminalProcess:
    """Reduced fallback: interactive pipes, explicitly not a PTY."""

    transport = "pipe_fallback"

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: dict[str, str],
        on_output: OutputCallback,
        degraded_reason: str,
    ) -> None:
        flags = 0
        if os.name == "nt":
            flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | CREATE_SUSPENDED
            )
        self.process = subprocess.Popen(
            list(argv), cwd=cwd, env=env, shell=False,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0,
            creationflags=flags, start_new_session=os.name != "nt",
        )
        self._process_tree = resume_owned_process(self.process, None)
        self.pid = int(self.process.pid)
        self.pid_started_at = process_started_at(self.pid)
        self.capabilities = {
            "true_pty": False,
            "ansi": False,
            "cursor_motion": False,
            "unicode": True,
            "resize": False,
            "foreground_interrupt": os.name != "nt",
            "backend_restart_survival": False,
            "ownership": "python_backend",
            "degraded_reason": str(degraded_reason),
        }
        self._on_output = on_output
        self._input = PipeInput(self.process.stdin)
        self._closed = False
        self._reader = PipeReader(
            self.process.stdout, lambda data: self._on_output("terminal", data),
            name=f"variant1-pipe-terminal-{self.pid}",
        )

    def write(self, data: bytes) -> int:
        return self._input.write(bytes(data))

    def resize(self, cols: int, rows: int) -> bool:
        return False

    def signal(self, name: str) -> bool:
        normalized = str(name or "").strip().lower()
        if normalized in {"interrupt", "ctrl_c", "sigint"}:
            if os.name == "nt" and hasattr(signal_module, "CTRL_BREAK_EVENT"):
                try:
                    self.process.send_signal(signal_module.CTRL_BREAK_EVENT)
                    return True
                except Exception:
                    return False
            try:
                os.killpg(self.process.pid, signal_module.SIGINT)
                return True
            except Exception:
                return False
        if normalized in {"terminate", "kill"}:
            self.terminate()
            return True
        raise ValueError(f"unsupported terminal signal: {name}")

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return int(self.process.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            return None

    def join_output(self, timeout: float = 2.0) -> bool:
        return self._reader.join(max(0.0, float(timeout)))

    def stop_output(self, timeout: float = 1.0) -> bool:
        return stop_readers([self._reader], timeout)

    def output_status(self) -> dict:
        return {"terminal": self._reader.status()}

    def terminate(self, *, force: bool = True) -> None:
        owner, self._process_tree = self._process_tree, None
        terminate_process_tree(self.process, owner=owner, force=force)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        owner, self._process_tree = self._process_tree, None
        if owner is not None:
            dispose_process_tree(
                owner, terminate=self.process.poll() is None
            )
        self.stop_output()
        self._input.close()


class PosixPtyProcess:
    """True PTY adapter for development on POSIX hosts."""

    transport = "posix_pty"
    capabilities = {
        "true_pty": True, "ansi": True, "cursor_motion": True,
        "unicode": True, "resize": True, "foreground_interrupt": True,
        "backend_restart_survival": False, "ownership": "python_backend",
    }

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: dict[str, str],
        cols: int,
        rows: int,
        on_output: OutputCallback,
    ) -> None:
        import pty
        master, slave = pty.openpty()
        self._master = master
        self._write_lock = threading.Lock()
        self._closed = False
        self._on_output = on_output
        self.process = subprocess.Popen(
            list(argv), cwd=cwd, env=env, shell=False,
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True,
        )
        self._process_tree = attach_process(None, self.process)
        os.close(slave)
        self.pid = int(self.process.pid)
        self.pid_started_at = process_started_at(self.pid)
        self.resize(cols, rows)
        self._reader = threading.Thread(
            target=self._read_loop, name=f"variant1-posix-pty-{self.pid}", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while self._master >= 0:
            try:
                data = os.read(self._master, 65536)
            except OSError:
                break
            if not data:
                break
            try:
                self._on_output("terminal", data)
            except Exception:
                continue

    def write(self, data: bytes) -> int:
        with self._write_lock:
            return os.write(self._master, bytes(data))

    def resize(self, cols: int, rows: int) -> bool:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self._master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", int(rows), int(cols), 0, 0))
        return True

    def signal(self, name: str) -> bool:
        normalized = str(name or "").strip().lower()
        if normalized in {"interrupt", "ctrl_c", "sigint"}:
            os.killpg(self.process.pid, signal_module.SIGINT)
            return True
        if normalized in {"terminate", "kill"}:
            self.terminate()
            return True
        raise ValueError(f"unsupported terminal signal: {name}")

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return int(self.process.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            return None

    def join_output(self, timeout: float = 2.0) -> bool:
        self._reader.join(timeout=max(0.0, float(timeout)))
        return not self._reader.is_alive()

    def terminate(self, *, force: bool = True) -> None:
        owner, self._process_tree = self._process_tree, None
        terminate_process_tree(self.process, owner=owner, force=force)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        owner, self._process_tree = self._process_tree, None
        if owner is not None:
            dispose_process_tree(
                owner, terminate=self.process.poll() is None
            )
        if self._master >= 0:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = -1


class StructuredChildProcess:
    """Pipe-backed deterministic stdout/stderr/exit process."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: dict[str, str],
        shell: bool,
        on_output: OutputCallback,
    ) -> None:
        flags = 0
        if os.name == "nt":
            flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | CREATE_SUSPENDED
            )
        command: str | list[str]
        command = str(argv[0]) if shell else list(argv)
        self.process = subprocess.Popen(
            command, cwd=cwd, env=env, shell=bool(shell),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, creationflags=flags, start_new_session=os.name != "nt",
        )
        self._process_tree = resume_owned_process(self.process, None)
        self._tree_lock = threading.RLock()
        self.pid = int(self.process.pid)
        self.pid_started_at = process_started_at(self.pid)
        self._on_output = on_output
        self._input = PipeInput(self.process.stdin)
        self._closed = False
        self._readers: dict[str, PipeReader] = {}
        for name, stream in (("stdout", self.process.stdout), ("stderr", self.process.stderr)):
            self._readers[name] = PipeReader(
                stream, lambda data, name=name: self._on_output(name, data),
                name=f"variant1-process-{name}-{self.pid}",
            )

    def write(self, data: bytes, *, should_stop=None) -> int:
        return self._input.write(bytes(data), should_stop=should_stop)

    def close_input(self) -> None:
        self._input.close()

    def join_output(self, timeout: float = 2.0) -> bool:
        return join_readers(self._readers.values(), timeout)

    def stop_output(self, timeout: float = 1.0) -> bool:
        return stop_readers(self._readers.values(), timeout)

    def output_status(self) -> dict:
        return {name: reader.status() for name, reader in self._readers.items()}

    def signal(self, name: str) -> bool:
        normalized = str(name or "").strip().lower()
        if normalized in {"interrupt", "ctrl_c", "sigint"}:
            if os.name == "nt" and hasattr(signal_module, "CTRL_BREAK_EVENT"):
                try:
                    self.process.send_signal(signal_module.CTRL_BREAK_EVENT)
                    return True
                except Exception:
                    return False
            try:
                os.killpg(self.process.pid, signal_module.SIGINT)
                return True
            except Exception:
                return False
        if normalized in {"terminate", "kill"}:
            self.terminate()
            return True
        raise ValueError(f"unsupported process signal: {name}")

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return int(self.process.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            return None

    def active_process_count(self) -> int:
        with self._tree_lock:
            owner = self._process_tree
            if owner is None:
                return int(self.process.poll() is None)
            return owner.active_process_count()

    def terminate(self, *, force: bool = True) -> None:
        with self._tree_lock:
            owner, self._process_tree = self._process_tree, None
        terminate_process_tree(self.process, owner=owner, force=force)

    def close(self, *, terminate_tree: bool = True) -> None:
        with self._tree_lock:
            if self._closed:
                return
            owner = self._process_tree
            if owner is not None and not terminate_tree and owner.active_process_count():
                raise RuntimeError('cannot release a live owned process tree without termination')
            self._closed = True
            self._process_tree = None
        if owner is not None:
            dispose_process_tree(owner, terminate=terminate_tree)
        self.stop_output()
        self.close_input()


def run_bounded_child(
    argv: Sequence[str],
    *,
    cwd: str,
    env: dict[str, str],
    input_bytes: bytes | None = None,
    timeout: float = 30.0,
    cancellation_requested: Callable[[], bool] | None = None,
    max_stdout_bytes: int = _DEFAULT_CHILD_OUTPUT_BYTES,
    max_stderr_bytes: int = _DEFAULT_CHILD_OUTPUT_BYTES,
    terminate_on_overflow: bool = True,
) -> BoundedChildResult:
    """Run one child while continuously bounding retained output.

    Reader threads always drain both pipes. Once a stream reaches its retained
    byte bound, later bytes are discarded and the main owner terminates the
    process tree by default. This keeps the memory bound real even when a child
    emits faster than the polling loop can observe it.
    """

    stdout = bytearray()
    stderr = bytearray()
    stdout_limit = max(0, int(max_stdout_bytes))
    stderr_limit = max(0, int(max_stderr_bytes))
    stdout_limit_exceeded = False
    stderr_limit_exceeded = False
    output_lock = threading.Lock()
    overflow = threading.Event()

    def on_output(stream: str, payload: bytes) -> None:
        nonlocal stdout_limit_exceeded, stderr_limit_exceeded
        value = bytes(payload)
        with output_lock:
            target = stderr if stream == "stderr" else stdout
            limit = stderr_limit if stream == "stderr" else stdout_limit
            remaining = max(0, limit - len(target))
            if remaining:
                target.extend(value[:remaining])
            if len(value) > remaining:
                if stream == "stderr":
                    stderr_limit_exceeded = True
                else:
                    stdout_limit_exceeded = True
                overflow.set()

    if cancellation_requested is not None and cancellation_requested():
        return BoundedChildResult(-1, b"", b"", cancelled=True)

    runtime = StructuredChildProcess(
        argv, cwd=cwd, env=env, shell=False, on_output=on_output,
    )
    timed_out = False
    cancelled = False
    deadline = time.monotonic() + max(0.1, float(timeout))

    def stop_feeding() -> bool:
        return (
            (cancellation_requested is not None and cancellation_requested())
            or time.monotonic() >= deadline
            or (terminate_on_overflow and overflow.is_set())
        )

    try:
        if input_bytes:
            # Feeding stdin is part of this explicitly bounded operation too.
            # A child that never reads cannot prevent entry to the owner loop
            # that performs exact tree termination and collects the result.
            runtime.write(input_bytes, should_stop=stop_feeding)
        runtime.close_input()
        code = None
        while code is None:
            if cancellation_requested is not None and cancellation_requested():
                cancelled = True
                runtime.terminate(force=True)
                code = runtime.wait(timeout=2.0)
                break
            if terminate_on_overflow and overflow.is_set():
                runtime.terminate(force=True)
                code = runtime.wait(timeout=2.0)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                runtime.terminate(force=True)
                code = runtime.wait(timeout=2.0)
                break
            code = runtime.wait(timeout=min(0.1, remaining))
        runtime.join_output(timeout=2.0)
        return BoundedChildResult(
            returncode=int(code if code is not None else -1),
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            timed_out=timed_out,
            cancelled=cancelled,
            stdout_limit_exceeded=stdout_limit_exceeded,
            stderr_limit_exceeded=stderr_limit_exceeded,
        )
    finally:
        if runtime.wait(timeout=0) is None:
            runtime.terminate(force=True)
        runtime.join_output(timeout=2.0)
        runtime.close()


def spawn_terminal(
    argv: Sequence[str],
    *,
    cwd: str,
    env: dict[str, str],
    cols: int,
    rows: int,
    on_output: OutputCallback,
    force_pipe_fallback: bool = False,
):
    if os.name == "nt" and not force_pipe_fallback:
        if conpty_available():
            try:
                return WindowsConPtyProcess(
                    argv, cwd=cwd, env=env, cols=cols, rows=rows,
                    on_output=on_output,
                )
            except Exception as exc:
                return PipeTerminalProcess(
                    argv, cwd=cwd, env=env, on_output=on_output,
                    degraded_reason=f"ConPTY launch failed: {type(exc).__name__}: {exc}",
                )
        return PipeTerminalProcess(
            argv, cwd=cwd, env=env, on_output=on_output,
            degraded_reason="ConPTY exports are unavailable on this Windows host",
        )
    if os.name != "nt" and not force_pipe_fallback:
        try:
            return PosixPtyProcess(
                argv, cwd=cwd, env=env, cols=cols, rows=rows, on_output=on_output)
        except Exception as exc:
            return PipeTerminalProcess(
                argv, cwd=cwd, env=env, on_output=on_output,
                degraded_reason=f"POSIX PTY launch failed: {type(exc).__name__}: {exc}",
            )
    return PipeTerminalProcess(
        argv, cwd=cwd, env=env, on_output=on_output,
        degraded_reason="pipe fallback explicitly requested",
    )


__all__ = [
    "BoundedChildResult", "PipeTerminalProcess", "PosixPtyProcess",
    "StructuredChildProcess", "inherited_environment", "process_started_at",
    "run_bounded_child", "spawn_terminal",
    "terminate_process_tree",
]
