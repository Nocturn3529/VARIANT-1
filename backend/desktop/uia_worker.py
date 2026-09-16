"""Dedicated COM-initialised UIA worker thread with hang watchdog.

UI Automation calls can block indefinitely on some Windows surfaces. Each job
is time-bounded and a timeout abandons that executor generation. Because Python
cannot kill the COM call safely, later UIA work stays blocked until the old call
returns (or the backend restarts), preventing concurrent desktop actors.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from typing import Any, Awaitable, Callable

import tools


class _DaemonSingleThreadExecutor:
    """Minimal Future executor whose hung worker cannot block interpreter exit.

    ``ThreadPoolExecutor`` registers every worker in a global atexit join set,
    including daemon-looking replacements. A COM call can be permanently
    blocked, so this owner deliberately uses one unregistered daemon thread.
    """

    def __init__(self, *, name: str) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._guard = threading.Lock()
        self._closed = False
        self.thread = threading.Thread(
            target=self._run,
            name=name,
            daemon=True,
        )
        self.thread.start()

    def submit(self, fn, /, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._guard:
            if self._closed:
                raise RuntimeError("UIA executor is shut down")
            self._queue.put((future, fn, args, kwargs))
        return future

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, fn, args, kwargs = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._guard:
            if not self._closed:
                self._closed = True
                if cancel_futures:
                    while True:
                        try:
                            pending = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        if pending is not None:
                            pending[0].cancel()
                self._queue.put(None)
        if wait:
            self.thread.join()


_UIA_EXEC: _DaemonSingleThreadExecutor | None = None
_COM_STATUS: str | None = None
_UIA_GENERATION = 0
_DEFAULT_JOB_TIMEOUT_S = 45.0
_RESTARTS = 0
_ABANDONED_WORK: list[concurrent.futures.Future] = []
_ABANDONED_LOCK = threading.Lock()
_DRAIN_TASKS: set[asyncio.Task] = set()


def _hung_work_pending() -> bool:
    with _ABANDONED_LOCK:
        _ABANDONED_WORK[:] = [work for work in _ABANDONED_WORK if not work.done()]
        return bool(_ABANDONED_WORK)


def com_begin() -> None:
    """Initialise COM (STA) on the current worker thread."""
    global _COM_STATUS
    import ctypes

    COINIT_APARTMENTTHREADED = 0x2
    try:
        hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        hr_u = hr & 0xFFFFFFFF
    except Exception as e:
        _COM_STATUS = f"CoInitializeEx call failed: {e}"
        return
    if hr_u in (0x00000000, 0x00000001, 0x80010106):
        _COM_STATUS = ""
    else:
        _COM_STATUS = f"CoInitializeEx returned HRESULT 0x{hr_u:08X}"


def executor() -> _DaemonSingleThreadExecutor:
    global _UIA_EXEC
    if _UIA_EXEC is None:
        _UIA_EXEC = _DaemonSingleThreadExecutor(name="variant1-uia")
        _UIA_EXEC.submit(com_begin).result(timeout=15)
    if _COM_STATUS:
        raise tools.ToolError(
            "desktop control couldn't initialise Windows COM on its worker thread "
            f"({_COM_STATUS}), so UI Automation is unavailable. This usually means a "
            "broken pywin32/comtypes install or a restricted session — reinstall the "
            "backend deps (npm run setup:backend) and retry.")
    return _UIA_EXEC


def restart_executor(*, reason: str = "") -> None:
    """Discard the hung worker pool so a later safe call can open a fresh thread.

    In-flight work on the old thread is abandoned via non-waiting
    ``shutdown(wait=False)``. The watchdog separately blocks subsequent UIA
    work while that abandoned future is still running.
    """
    global _UIA_EXEC, _COM_STATUS, _UIA_GENERATION, _RESTARTS
    old = _UIA_EXEC
    _UIA_EXEC = None
    _COM_STATUS = None
    _UIA_GENERATION += 1
    _RESTARTS += 1
    if old is not None:
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python < 3.9 cancel_futures
            try:
                old.shutdown(wait=False)
            except Exception:
                pass
        except Exception:
            pass
    print(
        f"[desktop] UIA worker restarted gen={_UIA_GENERATION} "
        f"restarts={_RESTARTS} reason={reason or 'watchdog'}",
        flush=True,
    )


def load_uia():
    """Import uiautomation on the UIA worker thread."""
    try:
        import uiautomation as auto
        return auto
    except Exception:
        raise tools.ToolError(
            "desktop control needs the 'uiautomation' package (Windows): "
            "pip install uiautomation",
        )


async def run(
    fn: Callable[[], Any],
    *,
    on_thread_start: Callable[[], None] | None = None,
    after_batch: Callable[[], Awaitable[None]] | None = None,
    timeout_s: float | None = None,
) -> Any:
    """Run a blocking UIA function on the dedicated COM thread.

    ``timeout_s`` bounds one job. On timeout the worker pool is restarted, but
    the abandoned COM call cannot be killed safely inside Python. Later UIA
    work is therefore blocked until that call actually returns (or VARIANT-1 is
    restarted), preventing two COM workers from acting on the desktop at once.

    A timed-out COM call cannot be killed in-process.
    """
    if _hung_work_pending():
        raise tools.ToolError(
            "desktop UI Automation is blocked because the previous timed-out "
            "UIA call is still running. Wait for the target app to recover, or "
            "restart VARIANT-1 if it remains hung."
        )
    limit = float(timeout_s if timeout_s is not None else _DEFAULT_JOB_TIMEOUT_S)
    limit = max(1.0, min(300.0, limit))

    def _run_on_uia_thread():
        if on_thread_start is not None:
            on_thread_start()
        return fn()

    loop = asyncio.get_running_loop()
    pool = executor()
    work = pool.submit(_run_on_uia_thread)
    wrapped = asyncio.wrap_future(work, loop=loop)
    try:
        result = await asyncio.wait_for(
            asyncio.shield(wrapped),
            timeout=limit,
        )
    except asyncio.CancelledError as cancellation:
        # Stop can release its caller even if COM is stuck. Retain a separate
        # quarantine until the exact native work AND its signal flush settle;
        # subsequent UIA requests fail promptly instead of waiting on an input
        # lock or racing a still-running native effect.
        pending = concurrent.futures.Future()
        with _ABANDONED_LOCK:
            _ABANDONED_WORK.append(pending)
        work.cancel()  # Prevent a queued, not-yet-running call from dispatching.
        restart_executor(reason="cancelled_job")

        async def drain():
            try:
                try:
                    await asyncio.shield(wrapped)
                except BaseException:
                    if not work.done():
                        return  # Loop shutdown must not advertise native completion.
                if after_batch is not None:
                    await after_batch()
            except BaseException:
                pass
            finally:
                if work.done():
                    pending.set_result(None)

        cleanup = asyncio.create_task(drain(), name="desktop-uia-cancel-drain")
        _DRAIN_TASKS.add(cleanup)
        cleanup.add_done_callback(_DRAIN_TASKS.discard)
        raise cancellation
    except asyncio.TimeoutError:
        if not work.done():
            with _ABANDONED_LOCK:
                _ABANDONED_WORK.append(work)
        restart_executor(reason=f"job_timeout_{limit:.0f}s")
        raise tools.ToolError(
            f"desktop UIA call timed out after {limit:.0f}s and the worker was "
            "abandoned. Further UIA actions are blocked until that call returns; "
            "restart VARIANT-1 if the target app remains hung."
        )
    if after_batch is not None:
        await after_batch()
    return result
