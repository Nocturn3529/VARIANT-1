"""Windows CI: the pinned cua-driver finds, observes, captures and drives one window.

Run after scripts/install-cua-driver.js, in an interactive desktop session.
The window is a small WinForms form this script owns (a text box and a
button with standard UI Automation), so a local run never touches the
user's own apps. The form reports its own events on stdout, so input is
checked by what the app received, not by what the driver says it sent.
An empty catalog is a failure; this does not accept the unsupported adapter.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time

sys.path.insert(0, "backend")

from desktop_fabric.cua_adapter import CuaDesktopAdapter  # noqa: E402
from desktop_fabric.cua_client import resolve_cua_driver_command  # noqa: E402
from desktop_fabric.models import DesktopElement  # noqa: E402

TITLE = "variant1-cua-smoke"
FORM = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.Form
$f.Text = '%s'
$f.Width = 420; $f.Height = 220; $f.StartPosition = 'Manual'; $f.Left = 60; $f.Top = 60
$t = New-Object System.Windows.Forms.TextBox
$t.Name = 'smokeText'; $t.Left = 20; $t.Top = 20; $t.Width = 300
$t.Add_TextChanged({ [Console]::Out.WriteLine('text:' + $t.Text); [Console]::Out.Flush() })
$b = New-Object System.Windows.Forms.Button
$b.Text = 'Confirm'; $b.Left = 20; $b.Top = 60; $b.Width = 120
$b.Add_Click({ [Console]::Out.WriteLine('clicked'); [Console]::Out.Flush() })
$f.Controls.Add($t); $f.Controls.Add($b)
$f.Add_Shown({ $p = $b.PointToScreen((New-Object System.Drawing.Point 60, 11)); [Console]::Out.WriteLine('button-at:' + $p.X + ':' + $p.Y); [Console]::Out.Flush() })
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 90000; $timer.Add_Tick({ $f.Close() }); $timer.Start()
[void]$f.ShowDialog()
""" % TITLE


def wait_for(lines: list[str], expected: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if expected in lines:
            return
        time.sleep(0.1)
    raise SystemExit(f"the form never reported {expected!r}; it reported {lines}")


def main() -> None:
    command = resolve_cua_driver_command()
    if not command:
        raise SystemExit("pinned cua-driver was not resolved")
    print("cua_driver_command", command)
    lines: list[str] = []
    form = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", FORM],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    threading.Thread(target=lambda: lines.extend(line.strip() for line in form.stdout),
                     daemon=True).start()
    adapter = CuaDesktopAdapter(command=command, platform="win32")
    try:
        deadline = time.monotonic() + 60
        window = None
        last: object = None
        while time.monotonic() < deadline:
            if form.poll() is not None:
                raise SystemExit(f"smoke form exited early: {form.stderr.read()}")
            _apps, windows = adapter.catalog(backend_instance_id="ci-desktop")
            last = [(item.title, item.pid) for item in windows]
            window = next((item for item in windows
                           if item.pid == form.pid and (item.title or "") == TITLE), None)
            if window is not None:
                break
            time.sleep(0.4)
        if window is None:
            raise SystemExit(f"smoke window missing from catalog={last}")

        focused = asyncio.run(adapter.focus(window))
        elements = list(focused.observation.uia if focused.observation else ())
        text_box = next((row for row in elements if str(row.get("role")).lower() == "edit"), None)
        button = next((row for row in elements if row.get("name") == "Confirm"), None)
        if text_box is None or button is None:
            raise SystemExit(f"observe lacks the text box or button: {elements}")

        def element(row):
            return DesktopElement(element_ref="smoke", observation_id="smoke", window_id=window.window_id,
                                  window_generation=1, element_generation=1, backend_key=row["backend_key"])

        def act(action, row, arguments):
            dispatched = asyncio.run(adapter.dispatch(window, action=action, delivery="auto",
                                                      element=element(row) if row else None,
                                                      arguments=arguments))
            if not dispatched.delivered:
                raise SystemExit(f"{action} was not delivered: {dispatched.metadata}")
            return dispatched

        act("type_text", text_box, {"text": "variant1"})
        wait_for(lines, "text:variant1")
        act("set_value", text_box, {"value": "by-value"})
        wait_for(lines, "text:by-value")
        act("click", button, {})
        wait_for(lines, "clicked")

        # Pixel input needs a screenshot from this session first.
        capture = asyncio.run(adapter.capture(window))
        if not capture.png.startswith(b"\x89PNG") or capture.width <= 0 or capture.height <= 0:
            raise SystemExit(f"capture did not return a PNG: {capture.width}x{capture.height}")
        at = next((line for line in lines if line.startswith("button-at:")), "")
        if not at:
            raise SystemExit(f"the form never reported its button position: {lines}")
        lines.remove("clicked")
        _label, x, y = at.split(":")
        act("click", None, {"x": int(x), "y": int(y)})
        wait_for(lines, "clicked")
        print("windows_desktop_ok", window.pid, window.hwnd, len(elements),
              f"{capture.width}x{capture.height}")
    finally:
        try:
            adapter.close()
        except Exception:
            pass
        if form.poll() is None:
            form.terminate()
            try:
                form.wait(timeout=5)
            except subprocess.TimeoutExpired:
                form.kill()


if __name__ == "__main__":
    main()
