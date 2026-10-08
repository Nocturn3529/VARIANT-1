"""Windows CI: pinned cua-driver lists, focuses, observes and captures one window.

Run after scripts/install-cua-driver.js, in an interactive desktop session.
The window is a small Tk window this script owns, so a local run never
touches the user's own apps. An empty catalog is a failure; this does not
accept the unsupported adapter.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, "backend")

from desktop_fabric.cua_adapter import CuaDesktopAdapter  # noqa: E402
from desktop_fabric.cua_client import resolve_cua_driver_command  # noqa: E402

TITLE = "variant1-cua-smoke"
WINDOW = (
    "import tkinter as tk\n"
    "root = tk.Tk()\n"
    f"root.title({TITLE!r})\n"
    "root.geometry('420x200+40+40')\n"
    "tk.Label(root, text='VARIANT-1 desktop smoke').pack()\n"
    "tk.Entry(root).pack()\n"
    "tk.Button(root, text='OK').pack()\n"
    "root.after(60000, root.destroy)\n"
    "root.mainloop()\n"
)


def main() -> None:
    command = resolve_cua_driver_command()
    if not command:
        raise SystemExit("pinned cua-driver was not resolved")
    print("cua_driver_command", command)
    window_proc = subprocess.Popen([sys.executable, "-c", WINDOW],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    adapter = CuaDesktopAdapter(command=command, platform="win32")
    window = None
    try:
        deadline = time.monotonic() + 30
        last: object = None
        while time.monotonic() < deadline:
            if window_proc.poll() is not None:
                _out, err = window_proc.communicate()
                raise SystemExit(f"smoke window exited early: {err.decode('utf-8', 'replace')}")
            _apps, windows = adapter.catalog(backend_instance_id="ci-desktop")
            last = [(item.title, item.pid) for item in windows]
            # Match the unique title: a venv's python.exe is a launcher whose
            # pid is not the interpreter that owns the window.
            window = next((item for item in windows if (item.title or "") == TITLE), None)
            if window is not None:
                break
            time.sleep(0.4)
        if window is None:
            raise SystemExit(f"smoke window missing from catalog={last}")
        focused = asyncio.run(adapter.focus(window))
        observation = focused.observation
        if observation is None or observation.completeness != "accessibility" or not observation.uia:
            raise SystemExit(f"observe did not return an accessibility view: {observation}")
        capture = asyncio.run(adapter.capture(window))
        if not capture.png.startswith(b"\x89PNG") or capture.width <= 0 or capture.height <= 0:
            raise SystemExit(f"capture did not return a PNG: {capture.width}x{capture.height}")
        print("windows_desktop_ok", window.pid, window.hwnd, window.title,
              len(observation.uia), f"{capture.width}x{capture.height}")
    finally:
        try:
            adapter.close()
        except Exception:
            pass
        if window is not None and window.pid != window_proc.pid:
            try:
                os.kill(int(window.pid), signal.SIGTERM)
            except OSError:
                pass
        if window_proc.poll() is None:
            window_proc.terminate()
            try:
                window_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                window_proc.kill()


if __name__ == "__main__":
    main()
