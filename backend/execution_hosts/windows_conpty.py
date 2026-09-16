"""Small direct Windows ConPTY adapter.

No optional Python PTY package is required.  Availability is determined from
the actual kernel32 ConPTY exports and every construction failure is surfaced
to the caller, which may choose an explicitly labelled pipe fallback.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

from process_tree import OwnedProcessTree, dispose_process_tree, process_started_at

OutputCallback = Callable[[str, bytes], None]


def _windows_environment_block(env: dict) -> str:
    """Build a NUL-separated Unicode environment block, rejecting injected NULs."""
    parts: list[str] = []
    for key, value in sorted((env or {}).items()):
        name = str(key)
        text = str(value)
        if "\x00" in name or "\x00" in text:
            raise ValueError("environment names and values must not contain NUL")
        if "=" in name:
            raise ValueError("environment names must not contain '='")
        parts.append(f"{name}={text}")
    return "\x00".join(parts) + "\x00\x00"


if os.name == "nt":
    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]


    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]


    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]


    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]


class ConPtyUnavailable(RuntimeError):
    pass


def conpty_available() -> bool:
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for name in ("CreatePseudoConsole", "ResizePseudoConsole", "ClosePseudoConsole"):
            getattr(kernel32, name)
        return True
    except Exception:
        return False


def _raise_last_error(operation: str) -> None:
    code = ctypes.get_last_error()
    raise OSError(code, f"{operation} failed", None, code)


def _check_hresult(value: int, operation: str) -> None:
    # HRESULT failure has the high bit set.  ctypes may expose it signed or
    # unsigned depending on the Python build.
    if int(value) & 0x80000000:
        raise OSError(int(value), f"{operation} failed with HRESULT 0x{int(value) & 0xffffffff:08x}")


class WindowsConPtyProcess:
    """One real pseudoconsole and its initial process."""

    transport = "conpty"
    capabilities = {
        "true_pty": True,
        "ansi": True,
        "cursor_motion": True,
        "unicode": True,
        "resize": True,
        "foreground_interrupt": True,
        "backend_restart_survival": False,
        "ownership": "python_backend",
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
        if not conpty_available():
            raise ConPtyUnavailable("Windows ConPTY exports are unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_functions()
        self._hpc = wintypes.HANDLE()
        self._process_handle = wintypes.HANDLE()
        self._input_handle = wintypes.HANDLE()
        self._output_handle = wintypes.HANDLE()
        self._pty_input_handle = wintypes.HANDLE()
        self._pty_output_handle = wintypes.HANDLE()
        self._closed = False
        self._process_tree: OwnedProcessTree | None = OwnedProcessTree()
        self._write_lock = threading.Lock()
        self._on_output = on_output
        self.pid = 0
        self.pid_started_at = 0.0
        try:
            self._spawn(tuple(str(item) for item in argv), cwd, env, cols, rows)
        except BaseException:
            owner, self._process_tree = self._process_tree, None
            if owner is not None:
                try:
                    dispose_process_tree(owner, terminate=True)
                except BaseException:
                    pass
            raise
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"variant1-conpty-reader-{self.pid}",
            daemon=True,
        )
        self._reader.start()

    def _configure_functions(self) -> None:
        k32 = self._kernel32
        k32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD,
        ]
        k32.CreatePipe.restype = wintypes.BOOL
        k32.SetHandleInformation.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ]
        k32.SetHandleInformation.restype = wintypes.BOOL
        k32.CreatePseudoConsole.argtypes = [
            COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        k32.CreatePseudoConsole.restype = ctypes.c_long
        k32.ResizePseudoConsole.argtypes = [wintypes.HANDLE, COORD]
        k32.ResizePseudoConsole.restype = ctypes.c_long
        k32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
        k32.ClosePseudoConsole.restype = None
        k32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
        ]
        k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        k32.DeleteProcThreadAttributeList.restype = None
        k32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
        ]
        k32.CreateProcessW.restype = wintypes.BOOL
        k32.ResumeThread.argtypes = [wintypes.HANDLE]
        k32.ResumeThread.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
        ]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.ReadFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        k32.ReadFile.restype = wintypes.BOOL
        k32.WriteFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        k32.WriteFile.restype = wintypes.BOOL
        k32.PeekNamedPipe.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.PeekNamedPipe.restype = wintypes.BOOL

    def _spawn(
        self, argv: tuple[str, ...], cwd: str, env: dict[str, str],
        cols: int, rows: int,
    ) -> None:
        k32 = self._kernel32
        sa = SECURITY_ATTRIBUTES(
            ctypes.sizeof(SECURITY_ATTRIBUTES), None, True,
        )
        pty_in_read = wintypes.HANDLE()
        parent_in_write = wintypes.HANDLE()
        parent_out_read = wintypes.HANDLE()
        pty_out_write = wintypes.HANDLE()
        handles: list[wintypes.HANDLE] = []
        attr_buffer: Any = None
        attr_initialized = False
        process_info = PROCESS_INFORMATION()
        try:
            if not k32.CreatePipe(
                ctypes.byref(pty_in_read), ctypes.byref(parent_in_write),
                ctypes.byref(sa), 0,
            ):
                _raise_last_error("CreatePipe(input)")
            handles.extend((pty_in_read, parent_in_write))
            if not k32.CreatePipe(
                ctypes.byref(parent_out_read), ctypes.byref(pty_out_write),
                ctypes.byref(sa), 0,
            ):
                _raise_last_error("CreatePipe(output)")
            handles.extend((parent_out_read, pty_out_write))
            HANDLE_FLAG_INHERIT = 0x00000001
            if not k32.SetHandleInformation(parent_in_write, HANDLE_FLAG_INHERIT, 0):
                _raise_last_error("SetHandleInformation(input)")
            if not k32.SetHandleInformation(parent_out_read, HANDLE_FLAG_INHERIT, 0):
                _raise_last_error("SetHandleInformation(output)")

            hr = k32.CreatePseudoConsole(
                COORD(int(cols), int(rows)), pty_in_read, pty_out_write, 0,
                ctypes.byref(self._hpc),
            )
            _check_hresult(hr, "CreatePseudoConsole")

            size = ctypes.c_size_t(0)
            k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            if not size.value:
                _raise_last_error("InitializeProcThreadAttributeList(size)")
            attr_buffer = ctypes.create_string_buffer(size.value)
            attr_ptr = ctypes.cast(attr_buffer, ctypes.c_void_p)
            if not k32.InitializeProcThreadAttributeList(
                attr_ptr, 1, 0, ctypes.byref(size)
            ):
                _raise_last_error("InitializeProcThreadAttributeList")
            attr_initialized = True
            PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
            if not k32.UpdateProcThreadAttribute(
                attr_ptr, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                self._hpc, ctypes.sizeof(wintypes.HANDLE), None, None,
            ):
                _raise_last_error("UpdateProcThreadAttribute(ConPTY)")

            startup = STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            # Explicit null standard handles prevent a debugger/redirected
            # parent console from winning over PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
            # on affected Windows builds (the same workaround used by node-pty
            # and the Windows Rust bindings).
            startup.StartupInfo.dwFlags |= 0x00000100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = wintypes.HANDLE()
            startup.StartupInfo.hStdOutput = wintypes.HANDLE()
            startup.StartupInfo.hStdError = wintypes.HANDLE()
            startup.lpAttributeList = attr_ptr
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            env_block = _windows_environment_block(env)
            env_buffer = ctypes.create_unicode_buffer(env_block)
            EXTENDED_STARTUPINFO_PRESENT = 0x00080000
            CREATE_UNICODE_ENVIRONMENT = 0x00000400
            CREATE_SUSPENDED = 0x00000004
            if not k32.CreateProcessW(
                None, command_line, None, None, False,
                EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
                | CREATE_SUSPENDED,
                ctypes.cast(env_buffer, ctypes.c_void_p), cwd,
                ctypes.cast(ctypes.byref(startup), ctypes.POINTER(STARTUPINFOW)),
                ctypes.byref(process_info),
            ):
                _raise_last_error("CreateProcessW(ConPTY)")
            self._process_handle = process_info.hProcess
            self.pid = int(process_info.dwProcessId)
            self.pid_started_at = process_started_at(self.pid)
            if self._process_tree is None:
                raise RuntimeError("ConPTY process-tree owner disappeared")
            self._process_tree.assign_pid(self.pid)
            if int(k32.ResumeThread(process_info.hThread)) == 0xFFFFFFFF:
                _raise_last_error("ResumeThread(ConPTY)")
            k32.CloseHandle(process_info.hThread)
            process_info.hThread = wintypes.HANDLE()

            # Keep the host-side pipe ends alive with the HPCON.  Some Windows
            # builds duplicate these internally while others retain the
            # caller's handle; retaining them is cheap and avoids premature
            # EOF on those hosts.
            self._pty_input_handle = pty_in_read
            self._pty_output_handle = pty_out_write
            handles.remove(pty_in_read)
            handles.remove(pty_out_write)
            self._input_handle = parent_in_write
            self._output_handle = parent_out_read
            handles.remove(parent_in_write)
            handles.remove(parent_out_read)
        except BaseException:
            if process_info.hThread:
                k32.CloseHandle(process_info.hThread)
            if process_info.hProcess:
                k32.TerminateProcess(process_info.hProcess, 1)
                k32.CloseHandle(process_info.hProcess)
            if self._hpc:
                k32.ClosePseudoConsole(self._hpc)
                self._hpc = wintypes.HANDLE()
            raise
        finally:
            if attr_initialized:
                k32.DeleteProcThreadAttributeList(
                    ctypes.cast(attr_buffer, ctypes.c_void_p))
            for handle in handles:
                if handle:
                    k32.CloseHandle(handle)

    def _read_loop(self) -> None:
        idle_after_exit = 0
        while self._output_handle and not self._closed:
            available = wintypes.DWORD()
            ok = self._kernel32.PeekNamedPipe(
                self._output_handle, None, 0, None,
                ctypes.byref(available), None,
            )
            if not ok:
                break
            if not available.value:
                if self.wait(timeout=0) is not None:
                    idle_after_exit += 1
                    if idle_after_exit >= 5:
                        break
                time.sleep(0.01)
                continue
            idle_after_exit = 0
            size = min(int(available.value), 65536)
            buffer = ctypes.create_string_buffer(size)
            read = wintypes.DWORD()
            if not self._kernel32.ReadFile(
                self._output_handle, buffer, size, ctypes.byref(read), None,
            ):
                break
            data = bytes(buffer.raw[:int(read.value)])
            if not data:
                continue
            try:
                self._on_output("terminal", data)
            except Exception:
                # Output collection failure must never stall the pseudoconsole.
                continue

    def write(self, data: bytes) -> int:
        raw = bytes(data)
        if not self._input_handle:
            raise BrokenPipeError("ConPTY input is closed")
        with self._write_lock:
            buffer = ctypes.create_string_buffer(raw, len(raw))
            written = wintypes.DWORD()
            if not self._kernel32.WriteFile(
                self._input_handle, buffer, len(raw), ctypes.byref(written), None,
            ):
                _raise_last_error("WriteFile(ConPTY input)")
            return int(written.value)

    def resize(self, cols: int, rows: int) -> bool:
        if not self._hpc:
            return False
        hr = self._kernel32.ResizePseudoConsole(
            self._hpc, COORD(int(cols), int(rows)))
        _check_hresult(hr, "ResizePseudoConsole")
        return True

    def signal(self, name: str) -> bool:
        normalized = str(name or "").strip().lower()
        if normalized in {"interrupt", "ctrl_c", "sigint"}:
            # Processed console input routes ETX to control handlers. Raw-mode
            # applications receive the character and decide whether to cancel,
            # clear input or exit. A successful write does not prove interruption.
            return self.write(b"\x03") == 1
        if normalized in {"terminate", "kill"}:
            self.terminate()
            return True
        raise ValueError(f"unsupported terminal signal: {name}")

    def wait(self, timeout: float | None = None) -> int | None:
        if not self._process_handle:
            return None
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, min(
            int(float(timeout) * 1000), 0xFFFFFFFE))
        result = int(self._kernel32.WaitForSingleObject(
            self._process_handle, milliseconds))
        WAIT_TIMEOUT = 0x00000102
        WAIT_OBJECT_0 = 0
        if result == WAIT_TIMEOUT:
            return None
        if result != WAIT_OBJECT_0:
            _raise_last_error("WaitForSingleObject")
        exit_code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(
            self._process_handle, ctypes.byref(exit_code)):
            _raise_last_error("GetExitCodeProcess")
        return int(exit_code.value)

    def terminate(self, *, force: bool = True) -> None:
        if not self.pid:
            return
        stopped = False
        owner, self._process_tree = self._process_tree, None
        if owner is not None:
            try:
                dispose_process_tree(owner, terminate=True)
                stopped = self.wait(timeout=2.0) is not None
            except Exception:
                stopped = False
        try:
            if not stopped:
                command = ["taskkill.exe", "/PID", str(self.pid), "/T"]
                if force:
                    command.append("/F")
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=5, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode == 0:
                    stopped = self.wait(timeout=2.0) is not None
        except Exception:
            stopped = False
        if force and not stopped and self._process_handle:
            self._kernel32.TerminateProcess(self._process_handle, 1)

    def close(self) -> None:
        if self._closed:
            return
        owner, self._process_tree = self._process_tree, None
        ownership_error: BaseException | None = None
        if owner is not None:
            try:
                dispose_process_tree(
                    owner, terminate=self.wait(timeout=0) is None
                )
            except BaseException as exc:
                ownership_error = exc
        # ClosePseudoConsole is synchronous.  Keep the reader pumping while it
        # flushes its final VT output to avoid the documented close deadlock.
        if self._hpc:
            self._kernel32.ClosePseudoConsole(self._hpc)
            self._hpc = wintypes.HANDLE()
        self._closed = True
        try:
            self._reader.join(timeout=1.0)
        except Exception:
            pass
        for attr in (
            "_input_handle", "_output_handle", "_pty_input_handle",
            "_pty_output_handle",
        ):
            handle = getattr(self, attr)
            if handle:
                self._kernel32.CloseHandle(handle)
                setattr(self, attr, wintypes.HANDLE())
        if self._process_handle:
            self._kernel32.CloseHandle(self._process_handle)
            self._process_handle = wintypes.HANDLE()
        if ownership_error is not None:
            raise ownership_error


__all__ = [
    "ConPtyUnavailable", "WindowsConPtyProcess", "conpty_available",
]
