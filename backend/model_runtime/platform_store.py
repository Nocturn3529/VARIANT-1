"""Small atomic JSON stores used by the local inference platform.

The inference control plane has a few independent durable documents (recipes,
remote nodes, downloaded model records, and benchmark history).  Keeping their
failure semantics in one place avoids each feature inventing a subtly different
partial-write or malformed-file policy.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
import tempfile
from threading import RLock
from typing import Any
from durable_document import DocumentLoadError, load_document


class AtomicJsonStore:
    """Thread-safe JSON document with preserved recovery and atomic replacement."""

    def __init__(self, path: str, default: Any):
        self.path = os.path.abspath(path)
        self.default = deepcopy(default)
        self._lock = RLock()
        self._load_failure = None

    def load(self) -> Any:
        with self._lock:
            try:
                value = load_document(self.path, self.default)
            except DocumentLoadError as exc:
                self._load_failure = exc
                raise
            self._load_failure = None
            return value

    def save(self, value: Any) -> None:
        with self._lock:
            if self._load_failure is not None:
                raise DocumentLoadError("Reload the durable document successfully before saving") from self._load_failure
            folder = os.path.dirname(self.path) or "."
            os.makedirs(folder, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                dir=folder,
                prefix=f".{os.path.basename(self.path)}-",
                suffix=".tmp",
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(value, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except Exception:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
                raise


__all__ = ["AtomicJsonStore"]
