"""One cross-process installer lock for the user's shared Grok plugin files."""
from __future__ import annotations

import os
from pathlib import Path
import time


def acquire(path, timeout=30, cancelled=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if path.stat().st_size == 0:
        stream.write(b"0")
        stream.flush()
    deadline = time.monotonic() + timeout
    while True:
        if cancelled is not None and cancelled.is_set():
            stream.close()
            raise InterruptedError("Grok adapter setup was cancelled")
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except OSError:
            if time.monotonic() >= deadline:
                stream.close()
                raise TimeoutError("Another Grok adapter setup is still running")
            time.sleep(.05)


def release(stream):
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()
