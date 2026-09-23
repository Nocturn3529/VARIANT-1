"""Linux CI: pinned cua-driver lists, focuses, and observes one X11 window.

Run under xvfb with a session bus, after scripts/install-cua-driver.js.
An empty window list is a failure; this does not accept the unsupported adapter.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

sys.path.insert(0, "backend")

from desktop_fabric.cua_adapter import CuaDesktopAdapter  # noqa: E402
from desktop_fabric.cua_client import resolve_cua_driver_command  # noqa: E402

TITLE = "variant1-cua-smoke"


def main() -> None:
    command = resolve_cua_driver_command()
    if not command:
        raise SystemExit("pinned cua-driver was not resolved")
    print("cua_driver_command", command)
    window_proc = subprocess.Popen(
        ["xmessage", "-title", TITLE, "-timeout", "60", "-buttons", "close:0", TITLE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    adapter = CuaDesktopAdapter(command=command, platform="linux")
    try:
        if window_proc.poll() is not None:
            _out, err = window_proc.communicate()
            raise SystemExit(f"xmessage exited early: {err.decode('utf-8', 'replace')}")
        adapter.open()
        deadline = time.monotonic() + 20
        window = None
        last: object = None
        while time.monotonic() < deadline:
            _apps, windows = adapter.catalog(backend_instance_id="ci-desktop")
            last = [(item.title, item.pid, item.hwnd) for item in windows]
            window = next((item for item in windows if TITLE in (item.title or "")), None)
            if window is not None:
                break
            time.sleep(0.4)
        if window is None:
            raw = adapter._tool("list_windows", {})
            raise SystemExit(f"smoke window missing from catalog={last} raw={raw!r}")
        focused = asyncio.run(adapter.focus(window))
        if int(focused.hwnd or 0) != int(window.hwnd):
            raise SystemExit(f"focus hwnd {focused.hwnd} != window {window.hwnd}")
        observation = focused.observation
        if observation is None or observation.completeness != "accessibility":
            raise SystemExit(f"observe did not return an accessibility view: {observation}")
        print(
            "linux_desktop_ok",
            window.pid,
            window.hwnd,
            window.title,
            len(observation.uia or ()),
        )
    finally:
        try:
            adapter.close()
        except Exception:
            pass
        if window_proc.poll() is None:
            window_proc.terminate()
            try:
                window_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                window_proc.kill()


if __name__ == "__main__":
    main()
