"""FIFO frame writes owned by a single CPython generation's stdin."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from queue import SimpleQueue
import threading
from typing import Any, BinaryIO

from .repl_protocol import ReplProtocolError, encode_line, request_frame


class FrameNotSent(ReplProtocolError):
    """The frame never reached an OS write."""


class FrameWriteUncertain(ReplProtocolError):
    """An attempted write or flush failed before delivery was confirmed."""


def check_write(write: Future[None]) -> None:
    if not write.done():
        raise FrameWriteUncertain("Worker stdin did not confirm the complete frame.")
    if write.cancelled():
        raise FrameNotSent("The frame was withdrawn before writing to the worker.")
    write.result()


class ReplFrameWriter:
    def __init__(self, stream: BinaryIO, *, pid: int, max_frame_bytes: int) -> None:
        self._stream = stream
        self._limit = max_frame_bytes
        self._queue: SimpleQueue = SimpleQueue()
        self._lock = threading.Lock()
        self._pending: set[Future[None]] = set()
        self._retired = False
        self._thread = threading.Thread(target=self._run, name=f"variant1-repl-writer:{pid}", daemon=True)
        self._thread.start()

    @property
    def retired(self) -> bool:
        with self._lock:
            return self._retired

    def submit(self, request_id: str, kind: str, fields: dict[str, Any]) -> Future[None]:
        write: Future[None] = Future()
        with self._lock:
            if self._retired:
                raise FrameNotSent("The worker input stream is retired.")
            self._pending.add(write)
            self._queue.put((write, request_id, kind, fields))
        return write

    def retire(self) -> None:
        with self._lock:
            if self._retired:
                return
            self._retired = True
            for write in self._pending:
                write.cancel()
            self._queue.put(None)

    async def wait_stopped(self) -> None:
        # The lease has already terminated its process tree. Keep ownership
        # on teardown failure, rather than closing a pipe under a live writer.
        await asyncio.to_thread(self._thread.join, 2.0)
        if self._thread.is_alive():
            raise RuntimeError("The retired worker input thread has not exited.")

    def _run(self) -> None:
        while (item := self._queue.get()) is not None:
            write, request_id, kind, fields = item
            try:
                if not write.cancelled():
                    self._write_frame(write, request_id, kind, fields)
            finally:
                with self._lock:
                    self._pending.discard(write)
                del item, fields

    def _write_frame(self, write: Future[None], request_id: str, kind: str, fields: dict[str, Any]) -> None:
        try:
            raw = encode_line(request_frame(request_id, kind, **fields), max_bytes=self._limit)
        except Exception as exc:
            if write.set_running_or_notify_cancel():
                write.set_exception(FrameNotSent(str(exc)))
            return
        # Cancellation can withdraw queued frames, including during encoding.
        # From this point a reader may have received bytes even on error.
        if not write.set_running_or_notify_cancel():
            return
        try:
            remaining = memoryview(raw)
            while remaining:
                if self.retired:
                    raise OSError("worker input retired during the frame write")
                count = self._stream.write(remaining)
                if count is None or not 0 < count <= len(remaining):
                    raise OSError("worker stdin did not accept a valid byte count")
                remaining = remaining[count:]
            self._stream.flush()
        except Exception as exc:
            # No subsequent frame may follow an incomplete JSON line.
            self.retire()
            write.set_exception(FrameWriteUncertain(str(exc) or type(exc).__name__))
        else:
            write.set_result(None)
