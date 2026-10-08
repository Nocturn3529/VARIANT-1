"""Backend-owned supervisor for the one cua-driver process.

The driver starts on first desktop use, not at backend startup. When the
process dies it is restarted on the next call with a new generation; an
action that was in flight is never replayed, because its effect is unknown.
Each VARIANT-1 run drives through its own driver session, which ends with
the run so held keys and the cursor overlay are released and late input
for that run is refused.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Any, Callable, Mapping

from .cua_client import CuaDriverClient, CuaDriverError, pinned_cua_driver_version


_DEFAULT_SESSION = "variant1"
_SESSION_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")


class CuaRunEnded(CuaDriverError):
    """Input for a run whose driver session has already ended."""


def session_label(run_id: str) -> str:
    clean = _SESSION_LABEL.sub("-", str(run_id or "")).strip("-")[:48]
    return f"variant1-{clean}" if clean else _DEFAULT_SESSION


class CuaDriverHost:
    def __init__(
        self,
        command: list[str],
        *,
        client_factory: Callable[[list[str]], Any] | None = None,
        expected_version: str | None = None,
    ) -> None:
        self.command = list(command)
        self._factory = client_factory or (lambda argv: CuaDriverClient(argv))
        self.expected_version = (
            pinned_cua_driver_version(self.command[0]) if expected_version is None
            else str(expected_version or "")
        )
        self.generation = 0
        self._client: Any = None
        self._lock = threading.RLock()
        self._sessions: set[str] = set()
        self._ended: OrderedDict[str, None] = OrderedDict()
        self._closed = False
        # Why the last start failed, for Settings; cleared by a good start.
        self.last_error = ""
        # macOS: the shared MacPermissionRequest, when the platform has one.
        self.permissions: Any = None

    @classmethod
    def from_client(cls, client: Any) -> "CuaDriverHost":
        """Wrap an already-built client (tests and embedders)."""

        host = cls(["cua-driver"], client_factory=lambda _argv: client, expected_version="")
        return host

    # Process lifecycle -------------------------------------------------

    def _ensure(self) -> Any:
        if self._closed:
            raise CuaDriverError("desktop driver is shut down")
        client = self._client
        alive = getattr(client, "alive", None)
        if client is not None and (not callable(alive) or alive()):
            return client
        if client is not None:
            self._discard(client)
        client = None
        try:
            client = self._factory(self.command)
            client.open()
            self._check_version(client)
        except Exception as exc:
            if client is not None:
                self._discard(client)
            self.last_error = str(exc)[:500]
            raise
        self.last_error = ""
        self.generation += 1
        self._client = client
        # A new process has no sessions; earlier runs simply get new ones.
        self._sessions.clear()
        return client

    def _check_version(self, client: Any) -> None:
        if not self.expected_version:
            return
        reported = str((getattr(client, "server_info", None) or {}).get("version") or "")
        if reported and reported != self.expected_version:
            raise CuaDriverError(
                f"cua-driver {reported} does not match the pinned {self.expected_version}; "
                "repair or reinstall VARIANT-1"
            )

    def _discard(self, client: Any) -> None:
        try:
            client.close()
        except Exception:
            pass
        if client is self._client:
            self._client = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            client = self._client
            self._client = None
            if client is not None:
                for label in list(self._sessions):
                    try:
                        client.call_tool("end_session", {"session": label})
                    except Exception:
                        pass
                self._discard(client)
            self._sessions.clear()

    def status(self) -> Mapping[str, Any]:
        client = self._client
        alive = getattr(client, "alive", None)
        return {
            "generation": self.generation,
            "running": bool(client is not None and (not callable(alive) or alive())),
            "expected_version": self.expected_version,
            "server_version": str((getattr(client, "server_info", None) or {}).get("version") or ""),
            "last_error": self.last_error,
        }

    # Calls -----------------------------------------------------------------

    def call(
        self, name: str, arguments: Mapping[str, Any], *,
        run_id: str = "", read_only: bool = False,
    ) -> dict[str, Any]:
        label = session_label(run_id)
        payload = dict(arguments)
        payload.setdefault("session", label)
        with self._lock:
            if run_id and label in self._ended:
                raise CuaRunEnded("this run has ended; its desktop input is refused")
            client = self._ensure()
            generation = self.generation
            try:
                result = client.call_tool(name, payload)
            except CuaDriverError:
                if read_only and not self._alive(client) and generation == self.generation:
                    # A read has no effect, so it may be retried once on a
                    # fresh process. Input is never replayed.
                    client = self._ensure()
                    result = client.call_tool(name, payload)
                else:
                    raise
            if run_id:
                self._sessions.add(label)
            return result

    @staticmethod
    def _alive(client: Any) -> bool:
        alive = getattr(client, "alive", None)
        return not callable(alive) or bool(alive())

    def end_run(self, run_id: str) -> None:
        """End one run's driver session; later input for it is refused."""

        if not run_id:
            return
        label = session_label(run_id)
        with self._lock:
            self._ended[label] = None
            self._ended.move_to_end(label)
            while len(self._ended) > 512:
                self._ended.popitem(last=False)
            client = self._client
            if label not in self._sessions or client is None or not self._alive(client):
                self._sessions.discard(label)
                return
            self._sessions.discard(label)
            try:
                client.call_tool("end_session", {"session": label})
            except Exception:
                pass


__all__ = ["CuaDriverHost", "CuaRunEnded", "session_label"]
