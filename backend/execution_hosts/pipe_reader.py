"""Cancellable output draining for anonymous subprocess pipes.

The reader is the only thread allowed to close its stream. Nonblocking reads
avoid cross-thread FileIO.close/read deadlocks, including inherited Windows
pipe handles whose writer outlives the process we own.
"""

from __future__ import annotations

import os
import threading
import time


class PipeReader:
    def __init__(self, stream, on_output, *, name: str) -> None:
        self.stream = stream
        self.on_output = on_output
        self.eof = stream is None
        self.error = ""
        self._stop = threading.Event()
        self._deadline = float("inf")
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        stream = self.stream
        if stream is None:
            return
        try:
            # Windows pipe support is in CPython 3.12+, including our 3.13
            # runtime. Never fall back to an uncancellable blocking read.
            os.set_blocking(stream.fileno(), False)
            while time.monotonic() < self._deadline:
                try:
                    data = stream.read(65536)
                except BlockingIOError:
                    data = None
                if data is None:
                    if self._stop.wait(0.02):
                        return
                    continue
                if not data:
                    self.eof = True
                    return
                try:
                    self.on_output(bytes(data))
                except Exception as exc:
                    self.error = f"{type(exc).__name__}: {exc}"[:500]
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            try:
                stream.close()
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:500]

    def request_stop(self, timeout: float = 1.0) -> None:
        # Drain immediately available bytes, but do not wait for another
        # writer or let continuous output make shutdown unbounded.
        self._deadline = min(self._deadline, time.monotonic() + max(0.0, timeout))
        self._stop.set()

    def join(self, timeout: float) -> bool:
        self.thread.join(timeout=max(0.0, timeout))
        return not self.thread.is_alive()

    def status(self) -> dict:
        return {
            "complete": self.eof and not self.error and not self.thread.is_alive(),
            "eof": self.eof,
            "reader_settled": not self.thread.is_alive(),
            **({"error": self.error} if self.error else {}),
        }


def join_readers(readers, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    return all([reader.join(max(0.0, deadline - time.monotonic())) for reader in readers])


def stop_readers(readers, timeout: float = 1.0) -> bool:
    for reader in readers:
        reader.request_stop(timeout)
    return join_readers(readers, timeout)


class PipeInput:
    """Serialize stdin writes without letting an idle child block closure."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def write(self, data: bytes, *, should_stop=None) -> int:
        with self._lock:
            if self.stream is None or self._closed.is_set():
                raise BrokenPipeError("process stdin is closed")
            os.set_blocking(self.stream.fileno(), False)
            data = memoryview(data)
            written = 0
            while written < len(data) and not self._closed.is_set():
                if should_stop is not None and should_stop():
                    break
                try:
                    count = self.stream.write(data[written:written + 65536])
                except BlockingIOError:
                    count = None
                if count:
                    written += count
                else:
                    self._closed.wait(0.02)
            return written

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            if self.stream is not None and not self.stream.closed:
                self.stream.close()
