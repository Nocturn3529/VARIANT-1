"""Cross-subsystem ownership for a spawned process and all descendants."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import threading
import time
from typing import Any


JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
CREATE_SUSPENDED = 0x00000004


def tcp_port_is_free(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_free_tcp_port(host: str) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def process_started_at(pid: int) -> float:
    try:
        import psutil

        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return time.time()


def win32_error(ctypes_module: Any, operation: str) -> OSError:
    number = int(ctypes_module.get_last_error() or 0)
    try:
        detail = str(ctypes_module.FormatError(number)).strip()
    except Exception:
        detail = f"Win32 error {number}"
    return OSError(number, f"{operation} failed: {detail}")


def add_error_note(error: BaseException, note: str) -> None:
    add_note = getattr(error, "add_note", None)
    if callable(add_note):
        add_note(note)


class OwnedProcessTree:
    """Windows Job Object owner with optional kernel-grade resource limits."""

    def __init__(
        self,
        *,
        max_processes: int = 0,
        process_memory_bytes: int = 0,
        job_memory_bytes: int = 0,
        cpu_percent: int = 0,
    ) -> None:
        self._is_windows = sys.platform.startswith("win")
        self._handle = None
        self._kernel32 = None
        self._ctypes = None
        self._closed = False
        self._handle_lock = threading.RLock()
        self._pgids: set[int] = set()
        if not self._is_windows:
            return

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class CpuRate(ctypes.Structure):
            _fields_ = [
                ("ControlFlags", wintypes.DWORD),
                ("CpuRate", wintypes.DWORD),
            ]

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise win32_error(ctypes, "CreateJobObjectW")
        try:
            limits = ExtendedLimits()
            flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if int(max_processes) > 0:
                flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                limits.BasicLimitInformation.ActiveProcessLimit = int(max_processes)
            if int(process_memory_bytes) > 0:
                flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
                limits.ProcessMemoryLimit = int(process_memory_bytes)
            if int(job_memory_bytes) > 0:
                flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
                limits.JobMemoryLimit = int(job_memory_bytes)
            limits.BasicLimitInformation.LimitFlags = flags
            if not kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise win32_error(ctypes, "SetInformationJobObject(process limits)")

            if int(cpu_percent) > 0:
                cpu = CpuRate()
                cpu.ControlFlags = (
                    JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                    | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
                )
                cpu.CpuRate = max(1, min(100, int(cpu_percent))) * 100
                if not kernel32.SetInformationJobObject(
                    handle,
                    JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION,
                    ctypes.byref(cpu),
                    ctypes.sizeof(cpu),
                ):
                    raise win32_error(ctypes, "SetInformationJobObject(CPU limit)")
        except BaseException:
            kernel32.CloseHandle(handle)
            raise

        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._handle = handle

    @property
    def active(self) -> bool:
        return bool(self._handle) if self._is_windows else not self._closed

    def assign_pid(self, pid: int) -> bool:
        clean_pid = int(pid)
        if clean_pid <= 0:
            raise ValueError("owned process pid must be positive")
        if not self._is_windows:
            if self._closed:
                raise RuntimeError("process-tree owner is closed")
            group = int(os.getpgid(clean_pid))
            if group != clean_pid:
                raise RuntimeError(
                    "POSIX owned process must start in its own session/process group"
                )
            self._pgids.add(group)
            return True
        if not self._handle or self._kernel32 is None or self._ctypes is None:
            raise RuntimeError("process-tree Job Object is unavailable or closed")
        process_handle = self._kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, clean_pid
        )
        if not process_handle:
            raise win32_error(self._ctypes, f"OpenProcess(pid={clean_pid})")

        assignment_error: BaseException | None = None
        try:
            if not self._kernel32.AssignProcessToJobObject(
                self._handle, process_handle
            ):
                raise win32_error(
                    self._ctypes,
                    f"AssignProcessToJobObject(pid={clean_pid})",
                )
        except BaseException as exc:
            assignment_error = exc

        close_error: BaseException | None = None
        try:
            if not self._kernel32.CloseHandle(process_handle):
                close_error = win32_error(
                    self._ctypes, f"CloseHandle(process pid={clean_pid})"
                )
        except BaseException as exc:
            close_error = exc
        if assignment_error is not None:
            if close_error is not None:
                add_error_note(assignment_error, str(close_error))
            raise assignment_error
        if close_error is not None:
            raise close_error
        return True

    def assign(self, process: Any) -> bool:
        try:
            pid = int(process.pid)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("owned subprocess must expose a positive integer pid") from exc
        return self.assign_pid(pid)

    def active_process_count(self) -> int:
        """Count living members of the owned group, including orphaned children."""
        with self._handle_lock:
            if self._closed:
                return 0
            if not self._is_windows:
                import psutil

                count = 0
                for process in psutil.process_iter(['pid', 'status']):
                    try:
                        if (process.info['status'] != psutil.STATUS_ZOMBIE
                                and os.getpgid(process.pid) in self._pgids):
                            count += 1
                    except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
                        continue
                return count
            if not self._handle:
                return 0
            # Job accounting can briefly retain an already-signalled process
            # (notably a Windows venv launcher). Count live members, not pending
            # kernel-object cleanup, before announcing an asynchronous handoff.
            members = self._windows_process_ids()
            for _pass in range(8):
                if not members:
                    return 0
                count = 0
                handles = []
                confirmed_exited = set()
                try:
                    for pid in members:
                        handle = self._kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                        if not handle:
                            if self._ctypes.get_last_error() == 87:
                                continue
                            raise win32_error(self._ctypes, 'OpenProcess(tree liveness)')
                        handles.append(handle)
                        state = self._kernel32.WaitForSingleObject(handle, 0)
                        if state == 258:  # WAIT_TIMEOUT: still running.
                            count += 1
                        elif state == 0:  # WAIT_OBJECT_0: exited.
                            confirmed_exited.add(pid)
                        else:
                            raise win32_error(self._ctypes, 'WaitForSingleObject(tree liveness)')
                    if count:
                        return count
                    # An observed parent may have forked and exited during the
                    # query. Re-read membership before declaring the tree empty.
                    # Holding its handles prevents PID reuse across this check.
                    current = self._windows_process_ids()
                    if set(current).issubset(confirmed_exited):
                        return 0
                finally:
                    close_error = None
                    for handle in handles:
                        if not self._kernel32.CloseHandle(handle):
                            close_error = close_error or win32_error(self._ctypes, 'CloseHandle(tree liveness)')
                    if close_error is not None:
                        raise close_error
                members = current
            # Missing process handles cannot pin PID identity. Rechecking
            # allows a reused PID to be seen as live, but never spin forever
            # on stale Job membership or claim uncertain ownership is empty.
            raise OSError('owned process tree membership did not stabilize')

    def _windows_process_ids(self) -> list[int]:
        """Read Job membership while the caller holds the handle lock."""
        from ctypes import wintypes

        capacity = 16
        for _pass in range(16):
            class ProcessIds(self._ctypes.Structure):
                _fields_ = [
                    ('NumberOfAssignedProcesses', wintypes.DWORD),
                    ('NumberOfProcessIdsInList', wintypes.DWORD),
                    ('ProcessIdList', self._ctypes.c_size_t * capacity),
                ]

            info = ProcessIds()
            queried = self._kernel32.QueryInformationJobObject(
                self._handle, 3, self._ctypes.byref(info),
                self._ctypes.sizeof(info), None,
            )
            if queried and info.NumberOfProcessIdsInList >= info.NumberOfAssignedProcesses:
                return [int(pid) for pid in info.ProcessIdList[:info.NumberOfProcessIdsInList]]
            if not queried and self._ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise win32_error(self._ctypes, 'QueryInformationJobObject(processes)')
            capacity = max(capacity * 2, int(info.NumberOfAssignedProcesses))
            if capacity > 1_048_576:
                break
        raise OSError('owned process tree membership query did not stabilize')

    def terminate(self, exit_code: int = 1) -> bool:
        with self._handle_lock:
            return self._terminate(exit_code)

    def _terminate(self, exit_code: int = 1) -> bool:
        if not self._is_windows:
            if self._closed:
                return False
            groups = tuple(self._pgids)
            first_error: BaseException | None = None
            for group in groups:
                try:
                    os.killpg(group, signal.SIGKILL if int(exit_code) else signal.SIGTERM)
                except ProcessLookupError:
                    self._pgids.discard(group)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    else:
                        add_error_note(first_error, str(exc))
            if first_error is not None:
                raise first_error
            return bool(groups)
        if not self._handle:
            return False
        if not self._kernel32.TerminateJobObject(self._handle, int(exit_code)):
            raise win32_error(self._ctypes, "TerminateJobObject")
        return True

    def close(self) -> bool:
        with self._handle_lock:
            return self._close()

    def _close(self) -> bool:
        if not self._is_windows:
            if self._closed:
                return False
            error: BaseException | None = None
            try:
                self.terminate()
            except BaseException as exc:
                error = exc
            self._pgids.clear()
            self._closed = True
            if error is not None:
                raise error
            return True
        if not self._handle:
            return False
        handle = self._handle
        self._handle = None
        self._closed = True
        if not self._kernel32.CloseHandle(handle):
            raise win32_error(self._ctypes, "CloseHandle(job)")
        return True

    def terminate_and_close(self) -> None:
        dispose_process_tree(self)


def attach_process(
    owner: OwnedProcessTree | None, process: Any,
    *, factory: type[OwnedProcessTree] = OwnedProcessTree,
) -> OwnedProcessTree | None:
    if process is None:
        return owner
    created = owner is None
    try:
        if owner is None:
            owner = factory()
        owner.assign(process)
    except BaseException as exc:
        poll = getattr(process, "poll", None)
        try:
            returncode = (
                poll() if callable(poll)
                else getattr(process, "returncode", None)
            )
        except BaseException:
            returncode = getattr(process, "returncode", None)
        if returncode is not None:
            # A very short child can exit between process creation and Job
            # assignment. Its pipes remain readable and no live tree remains.
            if created and owner is not None:
                try:
                    owner.close()
                except BaseException:
                    pass
            return None
        if returncode is None:
            try:
                process.kill()
            except BaseException as kill_error:
                add_error_note(exc, f"failed to kill unowned child: {kill_error}")
        if created and owner is not None:
            try:
                owner.close()
            except BaseException as close_error:
                add_error_note(exc, f"failed to close process-tree owner: {close_error}")
        raise
    return owner


async def attach_process_and_reap(
    owner: OwnedProcessTree | None,
    process: Any,
    *,
    factory: type[OwnedProcessTree] = OwnedProcessTree,
    reap_timeout_s: float = 2.0,
) -> OwnedProcessTree | None:
    try:
        return attach_process(owner, process, factory=factory)
    except BaseException as exc:
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                await asyncio.wait_for(wait(), timeout=max(0.1, reap_timeout_s))
            except BaseException as reap_error:
                add_error_note(exc, f"failed to reap unowned child: {reap_error}")
        raise


def resume_owned_process(process: Any, owner: OwnedProcessTree | None) -> OwnedProcessTree | None:
    """Assign a Windows suspended child before any of its code can run."""

    attached = attach_process(owner, process)
    if sys.platform != "win32":
        return attached
    if attached is None:
        return None
    try:
        import psutil

        psutil.Process(int(process.pid)).resume()
    except BaseException as exc:
        try:
            attached.terminate_and_close()
        except BaseException as cleanup_error:
            add_error_note(exc, f"failed to retire suspended child: {cleanup_error}")
        try:
            process.kill()
        except BaseException as cleanup_error:
            add_error_note(exc, f"failed to kill suspended child: {cleanup_error}")
        raise
    return attached


async def resume_owned_process_and_reap(
    process: Any, owner: OwnedProcessTree | None,
    *, reap_timeout_s: float = 2.0,
) -> OwnedProcessTree | None:
    try:
        return resume_owned_process(process, owner)
    except BaseException as exc:
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                await asyncio.wait_for(wait(), timeout=max(0.1, reap_timeout_s))
            except BaseException as reap_error:
                add_error_note(exc, f"failed to reap suspended child: {reap_error}")
        raise


async def settle_process(process: Any, *, timeout_s: float = 5.0) -> bool:
    """Wait for a child to settle despite repeated cancellation of cleanup."""

    wait = getattr(process, "wait", None)
    if not callable(wait):
        return False
    waiter = asyncio.create_task(wait(), name="owned-process-reap")
    deadline = asyncio.get_running_loop().time() + max(0.1, float(timeout_s))
    cancellation: asyncio.CancelledError | None = None
    completed = False
    while not waiter.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            done, _pending = await asyncio.wait({waiter}, timeout=remaining)
            completed = bool(done)
        except asyncio.CancelledError as exc:
            cancellation = exc
            continue
        if completed:
            break
    if not waiter.done():
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    else:
        waiter.result()
        completed = True
    if cancellation is not None:
        raise cancellation
    return completed


def dispose_process_tree(
    owner: OwnedProcessTree | None, *, terminate: bool = True
) -> bool:
    if owner is None:
        return False
    termination_error: BaseException | None = None
    if terminate:
        try:
            owner.terminate()
        except BaseException as exc:
            termination_error = exc
    close_error: BaseException | None = None
    try:
        owner.close()
    except BaseException as exc:
        close_error = exc
    if termination_error is not None:
        if close_error is not None:
            add_error_note(termination_error, str(close_error))
        raise termination_error
    if close_error is not None:
        raise close_error
    return True


__all__ = [
    "OwnedProcessTree",
    "PROCESS_SET_QUOTA",
    "CREATE_SUSPENDED",
    "PROCESS_TERMINATE",
    "add_error_note",
    "attach_process",
    "attach_process_and_reap",
    "resume_owned_process",
    "resume_owned_process_and_reap",
    "dispose_process_tree",
    "find_free_tcp_port",
    "process_started_at",
    "settle_process",
    "tcp_port_is_free",
    "win32_error",
]
