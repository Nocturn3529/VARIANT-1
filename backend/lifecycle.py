"""Startup utilities used by server.py's composition root.

Port-file handshake and FastAPI lifespan start/stop for background workers.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable


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


def dev_reset_on_launch(memory_store) -> None:
    """When VARIANT1_DEV_RESET=1, clear the approved-memory tables."""
    flag = os.environ.get("VARIANT1_DEV_RESET", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return
    try:
        memory_store.dev_reset()
        print("[dev] cleared memory", flush=True)
    except Exception as e:
        print(f"[dev] memory reset failed: {e}", flush=True)


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
