"""Minimal Linux CI smoke: unsupported desktop + Posix PTY spawn."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, "backend")

from desktop_fabric.unsupported import UnsupportedDesktopAdapter  # noqa: E402
from execution_hosts.local import spawn_terminal  # noqa: E402
from process_tree import OwnedProcessTree  # noqa: E402


def main() -> None:
    assert not OwnedProcessTree()._is_windows
    chunks: list[bytes] = []
    proc = spawn_terminal(
        ["/bin/echo", "ci-pty"],
        cwd="/tmp",
        env=dict(os.environ),
        cols=80,
        rows=24,
        on_output=lambda _stream, data: chunks.append(data),
    )
    assert type(proc).__name__ == "PosixPtyProcess"
    time.sleep(0.3)
    closer = getattr(proc, "close", None) or proc.terminate
    closer()
    print("linux_smoke_ok", UnsupportedDesktopAdapter.__name__, len(chunks))


if __name__ == "__main__":
    main()
