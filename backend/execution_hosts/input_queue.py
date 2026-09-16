"""Bounded stdin delivery owned by one exact live process/PTY generation."""
from __future__ import annotations

from collections import OrderedDict, deque
import threading
import uuid

from .models import ExecutionUnavailable, ExecutionValidationError


class InputQueue:
    def __init__(self, runtime, on_result, *, max_bytes=1024 * 1024, max_writes=64):
        self.runtime = runtime
        self.on_result = on_result
        self.max_bytes = max_bytes
        self.max_writes = max_writes
        self._condition = threading.Condition()
        self._queue = deque()
        self._receipts = OrderedDict()
        self._pending_bytes = 0
        self._active = None
        self._closed = False
        self._thread = None

    def submit(self, raw):
        raw = bytes(raw)
        with self._condition:
            if self._closed:
                raise ExecutionUnavailable("stdin is closed for this process generation")
            if self._pending_bytes + len(raw) > self.max_bytes or len(self._queue) + bool(self._active) >= self.max_writes:
                raise ExecutionValidationError("stdin backpressure: pending input queue is full; inspect delivery before retrying")
            identity = "input_" + uuid.uuid4().hex
            receipt = {"write_id": identity, "state": "queued", "accepted_bytes": len(raw),
                       "written_bytes": 0, "cancel_requested": False}
            self.on_result(dict(receipt))
            self._receipts[identity] = receipt
            self._queue.append((identity, raw))
            self._pending_bytes += len(raw)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="execution-stdin")
                self._thread.start()
            self._condition.notify()
            return dict(receipt)

    def status(self):
        with self._condition:
            ids = ([self._active] if self._active else []) + list(reversed(self._receipts))
            selected = list(dict.fromkeys(ids))[:16]
            return [dict(self._receipts[key]) for key in selected]

    def close(self):
        """Cancel undelivered writes; process termination releases any active OS write."""
        cancelled = []
        with self._condition:
            self._closed = True
            while self._queue:
                identity, raw = self._queue.popleft()
                self._pending_bytes -= len(raw)
                self._receipts[identity].update(state="cancelled", cancel_requested=True)
                cancelled.append(dict(self._receipts[identity]))
            if self._active:
                self._receipts[self._active].update(state="cancel_requested", written_bytes=None, cancel_requested=True)
            self._condition.notify_all()
        for receipt in cancelled:
            self._publish(receipt)

    def _publish(self, receipt):
        try:
            self.on_result(receipt)
        except Exception:
            # Delivery status stays queryable even if auxiliary action logging fails.
            import logging
            logging.getLogger(__name__).exception("stdin receipt logging failed")

    def _run(self):
        while True:
            with self._condition:
                while not self._queue and not self._closed:
                    self._condition.wait()
                if not self._queue:
                    return
                identity, raw = self._queue.popleft()
                self._active = identity
                self._receipts[identity].update(state="writing", written_bytes=None)
            try:
                written = int(self.runtime.write(raw))
                result = {"state": "written" if written == len(raw) else "partial", "written_bytes": written}
            except Exception as exc:
                result = {"state": "unknown_effect", "written_bytes": None, "error": str(exc), "error_type": type(exc).__name__}
            with self._condition:
                self._receipts[identity].update(result)
                receipt = dict(self._receipts[identity])
                self._pending_bytes -= len(raw)
                self._active = None
                while len(self._receipts) > 128:
                    self._receipts.popitem(last=False)
            self._publish(receipt)
