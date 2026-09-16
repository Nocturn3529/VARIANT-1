"""Minimal Linux CI smoke: unsupported desktop + Posix PTY spawn."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, "backend")

from desktop_fabric.models import DesktopUnavailable  # noqa: E402
from desktop_fabric.unsupported import UnsupportedDesktopAdapter  # noqa: E402
from execution_hosts.local import spawn_terminal  # noqa: E402
from process_tree import OwnedProcessTree  # noqa: E402


def main() -> None:
    assert not OwnedProcessTree()._is_windows

    adapter = UnsupportedDesktopAdapter(platform="linux")
    report = adapter.capability_report()
    assert report.get("supported") is False
    assert report.get("adapter") == "unsupported"
    raised = False
    try:
        adapter.catalog(backend_instance_id="ci-smoke")
    except DesktopUnavailable as exc:
        raised = True
        assert "not supported" in str(exc).lower() or "Win32/UIA" in str(exc)
    assert raised, "UnsupportedDesktopAdapter must raise DesktopUnavailable"

    chunks: list[bytes] = []
    proc = None
    try:
        proc = spawn_terminal(
            ["/bin/echo", "ci-pty"],
            cwd="/tmp",
            env=dict(os.environ),
            cols=80,
            rows=24,
            on_output=lambda _stream, data: chunks.append(data),
        )
        assert type(proc).__name__ == "PosixPtyProcess"
        deadline = time.monotonic() + 2.0
        blob = b""
        while time.monotonic() < deadline:
            blob = b"".join(chunks)
            if b"ci-pty" in blob:
                break
            time.sleep(0.05)
        blob = b"".join(chunks)
        assert b"ci-pty" in blob, f"missing sentinel in {blob!r} (parts={chunks!r})"
        code = proc.wait(timeout=2.0)
        assert code == 0, f"echo exit code {code}"
    finally:
        if proc is not None:
            closer = getattr(proc, "close", None)
            if callable(closer):
                closer()
            else:
                proc.terminate(force=True)

    print("linux_smoke_ok", UnsupportedDesktopAdapter.__name__, len(chunks))


if __name__ == "__main__":
    main()
