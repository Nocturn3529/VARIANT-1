"""First-use Playwright Chromium provisioning into VARIANT-1's user data."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable


class BrowserProvisionError(RuntimeError):
    pass


def runtime_root(value: str | None = None) -> Path:
    raw = str(value or os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if not raw:
        data = str(os.environ.get("VARIANT1_DATA_DIR") or "").strip()
        if not data:
            raise BrowserProvisionError("VARIANT1_DATA_DIR is required for browser provisioning")
        raw = os.path.join(data, "runtimes", "playwright")
    return Path(raw).expanduser().resolve()


def chromium_installed(value: str | None = None) -> bool:
    root = runtime_root(value)
    if not root.is_dir():
        return False
    chromium = any(
        path.is_file()
        for path in root.glob("chromium-*/INSTALLATION_COMPLETE")
    )
    headless = any(
        path.is_file()
        for path in root.glob("chromium_headless_shell-*/INSTALLATION_COMPLETE")
    )
    return chromium and headless


def runtime_status(value: str | None = None) -> dict[str, Any]:
    root = runtime_root(value)
    return {
        "installed": chromium_installed(str(root)),
        "path": str(root),
        "provisioning": not chromium_installed(str(root)),
    }


async def _install(
    root: Path,
    *,
    timeout_s: float,
    process_factory: Callable[..., Any] | None,
) -> Path:
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
    except ImportError as exc:
        raise BrowserProvisionError("Playwright driver is not installed") from exc

    root.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
    driver, cli = compute_driver_executable()
    env = get_driver_env()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
    create = process_factory or asyncio.create_subprocess_exec
    flags = 0x08000000 if sys.platform.startswith("win") else 0
    try:
        proc = await create(
            driver,
            cli,
            "install",
            "chromium",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=flags,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise BrowserProvisionError("Chromium first-use download timed out") from exc
    except BrowserProvisionError:
        raise
    except Exception as exc:
        raise BrowserProvisionError(f"could not start Chromium installer: {exc}") from exc

    text = bytes(output or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        detail = " ".join(text.split())[-1200:]
        raise BrowserProvisionError(
            f"Chromium first-use download failed ({proc.returncode})"
            + (f": {detail}" if detail else "")
        )
    if not chromium_installed(str(root)):
        raise BrowserProvisionError("Chromium installer completed without a usable runtime")
    return root


_install_tasks: dict[int, asyncio.Task] = {}


async def ensure_chromium_runtime(
    value: str | None = None,
    *,
    timeout_s: float = 1800.0,
    process_factory: Callable[..., Any] | None = None,
) -> Path:
    root = runtime_root(value)
    if chromium_installed(str(root)):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
        return root

    loop = asyncio.get_running_loop()
    key = id(loop)
    task = _install_tasks.get(key)
    if task is None or task.done():
        task = loop.create_task(
            _install(root, timeout_s=timeout_s, process_factory=process_factory)
        )
        _install_tasks[key] = task

        def cleanup(done: asyncio.Task) -> None:
            if _install_tasks.get(key) is done:
                _install_tasks.pop(key, None)
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(cleanup)
    return await asyncio.shield(task)


__all__ = [
    "BrowserProvisionError",
    "chromium_installed",
    "ensure_chromium_runtime",
    "runtime_root",
    "runtime_status",
]
