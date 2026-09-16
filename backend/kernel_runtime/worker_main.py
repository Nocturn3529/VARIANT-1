"""Minimal entry point frozen as ``Variant1Kernel.exe``.

Do not import ``server`` or host composition here.  The initial process waits
behind a one-use gate so its parent can establish Job Object ownership before
the CPython REPL, extensions, or model code can run.
"""

from __future__ import annotations

import os
import sys
import time


def _wait_for_parent_gate() -> None:
    path = os.environ.get("VARIANT1_KERNEL_GATE_FILE", "")
    token = os.environ.get("VARIANT1_KERNEL_GATE_TOKEN", "")
    if not path or not token:
        raise RuntimeError("VARIANT-1 kernel bootstrap gate is missing")
    deadline = time.monotonic() + float(
        os.environ.get("VARIANT1_KERNEL_GATE_TIMEOUT_S", "30")
    )
    while time.monotonic() < deadline:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                if handle.read() == token:
                    return
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    raise TimeoutError("VARIANT-1 host did not release the kernel bootstrap gate")


def _force_utf8_standard_streams() -> None:
    """Keep bootstrap diagnostics path-safe on non-UTF-8 Windows locales."""
    seen: set[int] = set()
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        if stream is None or id(stream) in seen:
            continue
        seen.add(id(stream))
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv: list[str] | None = None) -> int:
    _wait_for_parent_gate()
    _force_utf8_standard_streams()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--mutation-worker" in arguments:
        from kernel_runtime.mutation_worker import main as mutation_main

        return mutation_main()
    # Imports intentionally occur after the ownership gate. Source development
    # launches this file directly, so expose the backend package root first.
    backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if backend_root not in sys.path:
        sys.path.insert(0, backend_root)
    from kernel_runtime.repl_worker import main as repl_main

    return repl_main()


if __name__ == "__main__":
    raise SystemExit(main())
