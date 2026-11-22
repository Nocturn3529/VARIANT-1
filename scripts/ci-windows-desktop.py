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
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time

sys.path.insert(0, "backend")

from artifacts import ContentAddressedArtifactStore  # noqa: E402
from desktop_fabric import DesktopFabric, create_desktop_fabric  # noqa: E402
from desktop_fabric.cua_adapter import CuaDesktopAdapter  # noqa: E402
from desktop_fabric.cua_client import resolve_cua_driver_command  # noqa: E402

FORM = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.Form
$f.Text = '%s'
$f.Width = 420; $f.Height = 220; $f.StartPosition = 'Manual'; $f.Left = 60; $f.Top = 60
$t = New-Object System.Windows.Forms.TextBox
$t.Name = 'smokeText'; $t.AccessibleName = 'Smoke input'; $t.Left = 20; $t.Top = 20; $t.Width = 300
$t.Add_TextChanged({ [Console]::Out.WriteLine('text:' + $t.Text); [Console]::Out.Flush() })
$b = New-Object System.Windows.Forms.Button
$b.Text = 'Confirm'; $b.Left = 20; $b.Top = 60; $b.Width = 120
$script:clicks = 0
$b.Add_Click({
    $script:clicks++
    $t.Text = 'click-' + $script:clicks
    [Console]::Out.WriteLine('clicked:' + $script:clicks)
    [Console]::Out.Flush()
})
$f.Controls.Add($t); $f.Controls.Add($b)
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 90000; $timer.Add_Tick({ $f.Close() }); $timer.Start()
[void]$f.ShowDialog()
"""


async def wait_for(lines: list[str], expected: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if expected in lines:
            return
        await asyncio.sleep(0.1)
    raise SystemExit(f"the form never reported {expected!r}; it reported {lines}")


def main() -> None:
    command = resolve_cua_driver_command()
    if not command:
        raise SystemExit("pinned cua-driver was not resolved")
    print("cua_driver_command", command)
    with TemporaryDirectory(prefix="variant1-cua-smoke-") as directory:
        asyncio.run(run_smoke(command, Path(directory)))


async def run_smoke(command: list[str], directory: Path) -> None:
    title = directory.name
    lines: list[str] = []
    adapter = CuaDesktopAdapter(command=command, platform="win32")
    fabric = create_desktop_fabric(
        path=str(directory / "desktop.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(str(directory / "artifacts")),
        adapter=adapter, backend_instance_id="ci-desktop",
    )
    try:
        with subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", FORM % title],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as form:
            reader = threading.Thread(
                target=lambda: lines.extend(line.strip() for line in form.stdout), daemon=True,
            )
            reader.start()
            try:
                await exercise_form(fabric, form, title, lines)
            finally:
                if form.poll() is None:
                    form.terminate()
                    try:
                        form.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        form.kill()
                        form.wait()
                reader.join(timeout=5)
    finally:
        fabric.close()


async def exercise_form(
    fabric: DesktopFabric, form: subprocess.Popen[str], title: str, lines: list[str],
) -> None:
    deadline = time.monotonic() + 60
    window = None
    while time.monotonic() < deadline:
        if form.poll() is not None:
            raise SystemExit(f"smoke form exited early: {form.stderr.read()}")
        _apps, windows = fabric.refresh_catalog()
        window = next((item for item in windows if item.pid == form.pid and item.title == title), None)
        if window is not None:
            break
        await asyncio.sleep(0.4)
    if window is None:
        raise SystemExit(f"smoke form PID {form.pid} missing from catalog")

    view = await fabric.observe(window, mode="uia", include_image=False)
    text_box = next((item for item in view.elements if item.role.casefold() == "edit"), None)
    if text_box is None or not any(item.name == "Confirm" for item in view.elements):
        raise SystemExit("smoke form observation lacks the text box or Confirm button")

    async def act(action, target, arguments, value, event):
        source = await fabric.observe(window, mode="uia", include_image=True)
        if not source.image_ref:
            raise SystemExit(f"smoke form screenshot unavailable: {source.capture.get('error')}")
        result = await fabric.act(
            window, action, target=target, arguments=arguments, include_image_evidence=False,
            expect={"element": {"ref": text_box.element_ref, "value": value}},
            source_observation=source,
        )
        receipt = fabric.repository.get_operation(result.operation_id)
        if receipt.state != "verified" or receipt.dispatch.get("input_sent") is not True:
            raise SystemExit(f"{action} receipt {receipt.state}: {receipt.error} {receipt.evidence}")
        await wait_for(lines, event)
        return receipt.dispatch["selected_delivery"]

    typed = await act("type_text", text_box.element_ref, {"text": "variant1"}, "variant1", "text:variant1")
    value = await act("set_value", text_box.element_ref, {"value": "by-value"}, "by-value", "text:by-value")
    if value != "semantic":
        raise SystemExit(f"expected semantic set_value receipt, got {value}")
    clicked = await act("click", "Confirm", {}, "click-1", "clicked:1")

    # UI Automation provides physical screen bounds for this control. Use the
    # same coordinate space as Desktop Fabric's capture and click arguments.
    view = await fabric.observe(window, mode="uia", include_image=False)
    button = next((item for item in view.elements if item.name == "Confirm"), None)
    if button is None or button.bounds is None:
        raise SystemExit("Confirm lacks a bounded UI Automation frame")
    left, top, right, bottom = button.bounds
    x, y = (left + right) // 2, (top + bottom) // 2
    capture = await fabric.capture(window)
    png = fabric.artifact_store.read_bytes(capture.artifact_ref)
    origin_x, origin_y = capture.coordinate_transform["screen_origin"]
    if not (png.startswith(b"\x89PNG") and 0 <= x - origin_x < capture.width
            and 0 <= y - origin_y < capture.height):
        raise SystemExit(f"Confirm frame {button.bounds} falls outside the window PNG")
    point = await act("click", None, {"x": x, "y": y}, "click-2", "clicked:2")
    print("windows_desktop_ok", window.pid, window.hwnd, len(view.elements),
          f"{capture.width}x{capture.height}", f"button={x},{y}",
          f"delivery={typed},{value},{clicked},{point}")


if __name__ == "__main__":
    main()
