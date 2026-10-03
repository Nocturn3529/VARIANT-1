"""Lifetime ownership of kernel scratch and cell-evidence storage.

OS locks are authoritative and are never unlinked during reclaim. PID identity
is additional evidence for old generations/admissions, not a lock substitute.
"""
from __future__ import annotations

import json
import math
import os
import time
from contextlib import suppress
from typing import Any

from .contracts import KernelUnavailable


OWNER_SCHEMA = "variant1.kernel-owner.v1"
# An abandoned manager must not surrender storage while its workers might
# remain alive. Only explicit settled shutdown or process exit releases it.
_LIVE_OWNERS: set["KernelStorageOwnership"] = set()


def process_identity(instance_id: str) -> dict[str, Any]:
    import psutil

    return {"schema": OWNER_SCHEMA, "instance_id": str(instance_id),
            "pid": os.getpid(), "created_at": psutil.Process().create_time()}


def owner_state(owner: Any) -> str:
    """Return live/dead/unknown, preserving inaccessible or unattributed data."""
    import psutil

    if not isinstance(owner, dict) or owner.get("schema") != OWNER_SCHEMA:
        return "unknown"
    pid, created = owner.get("pid"), owner.get("created_at")
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            or isinstance(created, bool) or not isinstance(created, (int, float))
            or not math.isfinite(float(created)) or not float(created) > 0):
        return "unknown"
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - float(created)) > .001:
            return "dead"  # The PID now belongs to another process.
        return "dead" if process.status() == psutil.STATUS_ZOMBIE else "live"
    except psutil.NoSuchProcess:
        return "dead"
    except (psutil.Error, OSError):
        return "unknown"


class KernelStorageOwnership:
    def __init__(self, root: str, ledger_path: str, instance_id: str, *, restrict):
        self.identity = process_identity(instance_id)
        self.prior_instances: set[str] = set()
        self._streams = []
        paths = sorted({os.path.realpath(os.path.abspath(path)) + ".owner.lock"
                        for path in (root, ledger_path)})
        try:
            for path in paths:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                stream = os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+b", buffering=0)
                try:
                    self._lock(stream)
                except OSError as exc:
                    stream.close()
                    raise KernelUnavailable("Kernel storage is already owned or inaccessible: " + path) from exc
                self._streams.append(stream)
                restrict(path)
                # Byte zero is the Windows lock region; metadata starts after it
                # so diagnostics do not need to read a rival's locked byte.
                stream.seek(1)
                prior_raw = stream.read(8192)
                try:
                    prior = json.loads(prior_raw) if prior_raw else {}
                except (ValueError, UnicodeError):
                    prior = {}
                if (isinstance(prior, dict) and prior.get("schema") == OWNER_SCHEMA
                        and prior.get("instance_id") and (prior.get("state") == "released"
                        or owner_state(prior) == "dead")):
                    # Acquiring the same lifetime resource lock proves the prior
                    # cooperating owner released it (or exited), even if its PID
                    # is still live because it closed a manager in this process.
                    self.prior_instances.add(str(prior["instance_id"]))
                self._write(stream, {**self.identity, "state": "owned", "acquired_at": time.time()})
            _LIVE_OWNERS.add(self)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _lock(stream) -> None:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _write(stream, document: dict) -> None:
        stream.seek(0)
        payload = b"\0" + json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
        written = stream.write(payload)
        if written != len(payload):
            raise OSError("Incomplete kernel-owner metadata write")
        stream.truncate()
        os.fsync(stream.fileno())

    def close(self) -> None:
        for stream in reversed(self._streams):
            with suppress(Exception):
                self._write(stream, {**self.identity, "state": "released", "released_at": time.time()})
            stream.close()  # Closing releases the OS lock; never unlink its inode.
        self._streams.clear()
        _LIVE_OWNERS.discard(self)
