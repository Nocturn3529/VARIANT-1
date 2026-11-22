"""Startup utilities used by server.py's composition root.

Port-file handshake and FastAPI lifespan start/stop for background workers.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

import psutil


def owns_backend_process(arguments: list[str]) -> bool:
    """Read-only check for Electron before it uses a PID from a saved handshake."""
    if len(arguments) != 4:
        return False
    pid, started_at, instance_id, port_file = arguments
    try:
        process = psutil.Process(int(pid))
        if (process.pid <= 1 or not instance_id or not os.path.isabs(port_file)
                or process.create_time() != float(started_at)):
            return False
        if not os.path.samefile(process.exe(), psutil.Process().exe()):
            return False
        args = process.cmdline()[1:]
        if not getattr(sys, "frozen", False):
            server = os.path.join(os.path.dirname(__file__), "server.py")
            if not args or not os.path.isabs(args[0]) or not os.path.samefile(args[0], server):
                return False
            args = args[1:]
        if len(args) != 2 or args[0] != "--port-file" or not os.path.isabs(args[1]):
            return False
        actual_path = os.path.normcase(os.path.realpath(args[1]))
        return (actual_path == os.path.normcase(os.path.realpath(port_file))
                and process.environ().get("VARIANT1_BACKEND_INSTANCE_ID") == instance_id
                and process.is_running())
    except (psutil.Error, OSError, ValueError, OverflowError):
        return False


def write_port_file(path: str, payload: dict) -> None:
    """Atomically write the backend identity handshake Electron polls."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".port-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise




def make_lifespan(
    *,
    runtime: dict,
    auth_token: Callable[[], str],
    version: str,
    start_workers: Callable[[], list | Awaitable[list]],
    shutdown: Callable[[], Awaitable[None]],
    activity_token: Callable[[], str] | None = None,
):
    """Build a FastAPI lifespan context for the composition root.

    ``runtime`` is the live ``_RUNTIME`` dict (host/port/port_file filled at main()).
    """

    @asynccontextmanager
    async def lifespan(app: Any):
        runtime["ready"] = False
        runtime["startup_error"] = ""
        port_file = runtime.get("port_file")
        if port_file:
            payload = {
                "port": runtime.get("port"),
                "token": auth_token(),
                "pid": os.getpid(),
                "version": version,
                "instance_id": runtime.get("instance_id"),
            }
            if activity_token is not None:
                payload["activity_token"] = activity_token()
            try:
                payload["process_started_at"] = psutil.Process().create_time()
            except (psutil.Error, OSError):
                pass  # Without OS birth evidence Electron skips record-only force termination.
            write_port_file(port_file, payload)
            print(
                f"[variant1-backend] listening on {runtime.get('host')}:{runtime.get('port')}",
                flush=True,
            )
        tasks = []
        try:
            started = start_workers()
            if inspect.isawaitable(started):
                started = await started
            tasks = list(started or [])
            runtime["ready"] = True
            yield
        except Exception as exc:
            runtime["ready"] = False
            runtime["startup_error"] = str(exc)
            if port_file:
                try:
                    os.remove(port_file)
                except FileNotFoundError:
                    pass
                except Exception:
                    pass
            raise
        finally:
            runtime["ready"] = False
            for task in tasks:
                try:
                    task.cancel()
                except Exception:
                    pass
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await shutdown()

    return lifespan
