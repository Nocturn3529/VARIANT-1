"""macOS: drive the desktop through trycua's signed CuaDriver.app.

macOS gives Accessibility and Screen Recording to the responsible app. VARIANT-1
has no Developer ID, so grants made to it would not survive its updates. As
Hermes does, VARIANT-1 therefore runs the pinned driver as trycua's signed
CuaDriver.app (com.trycua.driver): its private daemon starts through
LaunchServices (``open -a``), which makes CuaDriver.app the responsible
process, so the user grants CuaDriver once and the grants outlive VARIANT-1
updates. The bundle must carry trycua's signature before every launch. The
MCP stdio proxy talks only to that daemon, over a private socket.

The daemon runs with its permission gate off so it starts the same way every
time. When it reports missing grants, VARIANT-1 launches CuaDriver.app once
more with the gate on, which is how upstream's ``permissions grant`` asks
macOS for them, and restarts the daemon once they are given.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from .cua_client import CuaDriverClient, CuaDriverError, cua_driver_env


BUNDLE_ID = "com.trycua.driver"
# Teams that sign trycua's official releases. Exact matches only.
TEAM_IDS = ("4YEC26S9KF", "YCK386LBJ7")
_START_TIMEOUT_S = 15.0
_GRANT_TIMEOUT_S = 300.0
# Only these reach the LaunchServices-started daemon (``open --env``); it does
# not inherit VARIANT-1's environment.
_DAEMON_ENV_KEYS = ("CUA_DRIVER_RS_TELEMETRY_ENABLED", "DO_NOT_TRACK")
PERMISSION_HINT = (
    "Allow CuaDriver in System Settings > Privacy & Security under Accessibility "
    "and Screen Recording, then try again."
)

Run = Callable[..., Any]


def app_bundle_for(binary: str) -> str | None:
    """The CuaDriver.app that carries ``binary``, judged from its real path only."""

    head, marker, _ = os.path.realpath(binary).partition(os.path.join(".app", "Contents", "MacOS", ""))
    if not marker:
        return None
    app = head + ".app"
    executable = os.path.join(app, "Contents", "MacOS", "cua-driver")
    return app if os.path.isfile(executable) and os.access(executable, os.X_OK) else None


def verify_app_signature(app_path: str, *, run: Run = subprocess.run) -> None:
    """Refuse to launch anything but trycua's intact, signed CuaDriver.app."""

    codesign = shutil.which("codesign") or "/usr/bin/codesign"
    try:
        intact = run([codesign, "--verify", "--strict", app_path],
                     capture_output=True, text=True, timeout=30)
        shown = run([codesign, "-dv", app_path], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CuaDriverError(f"could not verify CuaDriver.app's signature: {exc}") from exc
    if intact.returncode != 0 or shown.returncode != 0:
        detail = ((intact.stderr or "") + (shown.stderr or "")).strip()[:300]
        raise CuaDriverError(
            f"CuaDriver.app's signature is missing or broken ({detail}); "
            "repair or reinstall VARIANT-1"
        )
    fields: dict[str, str] = {}
    for line in (shown.stderr or "").splitlines():  # codesign -dv reports on stderr
        key, sep, value = line.partition("=")
        if sep and key.strip() not in fields:
            fields[key.strip()] = value.strip()
    identifier, team = fields.get("Identifier", ""), fields.get("TeamIdentifier", "")
    if identifier != BUNDLE_ID or team not in TEAM_IDS:
        raise CuaDriverError(
            f"CuaDriver.app is signed as {identifier or '?'} by team {team or '?'}, "
            f"not trycua's {BUNDLE_ID}; repair or reinstall VARIANT-1"
        )


class MacDriverDaemon:
    """One LaunchServices-started CuaDriver.app daemon on a private socket."""

    def __init__(
        self,
        binary: str,
        *,
        env: Mapping[str, str],
        gate: bool = False,
        run: Run = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        socket_dir: str | None = None,
    ) -> None:
        self.binary = binary
        self.app = app_bundle_for(binary)
        self.env = dict(env)
        self.gate = gate
        token = uuid.uuid4().hex[:12]
        # Unix sockets allow ~104 bytes on macOS; the temp dir keeps it short.
        self.socket_path = os.path.join(socket_dir or tempfile.gettempdir(), f"v1cua-{token}.sock")
        self._run = run
        self._sleep = sleep
        self._clock = clock
        self._launched = False
        self._stop_requested = False

    def serve_arguments(self) -> list[str]:
        if self.gate:
            # Standalone with its permission gate: CuaDriver asks macOS for
            # the grants and opens the socket once they are given.
            return ["serve", "--socket", self.socket_path, "--no-overlay"]
        return ["serve", "--embedded", "--socket", self.socket_path,
                "--no-permissions-gate", "--no-overlay"]

    def launch_command(self) -> list[str]:
        env_flags: list[str] = []
        for key in _DAEMON_ENV_KEYS:
            if key in self.env:
                env_flags += ["--env", f"{key}={self.env[key]}"]
        return ["/usr/bin/open", "-n", "-g", *env_flags, "-a", str(self.app),
                "--args", *self.serve_arguments()]

    def proxy_command(self) -> list[str]:
        # --embedded: the proxy never launches a daemon of its own by app name.
        return [self.binary, "mcp", "--embedded", "--socket", self.socket_path]

    def launch(self) -> None:
        if not self.app:
            raise CuaDriverError("macOS desktop control needs CuaDriver.app; repair or reinstall VARIANT-1")
        verify_app_signature(self.app, run=self._run)
        try:
            opened = self._run(self.launch_command(), capture_output=True, text=True,
                               timeout=30, env=self.env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CuaDriverError(f"could not start CuaDriver.app: {exc}") from exc
        if opened.returncode != 0:
            raise CuaDriverError(
                f"could not start CuaDriver.app: {(opened.stderr or '').strip()[:300] or opened.returncode}"
            )
        self._launched = True

    def wait_listening(self, timeout_s: float) -> bool:
        deadline = self._clock() + timeout_s
        while True:
            if self._stop_requested:
                return False
            if self.listening():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(0.2)

    def start(self) -> None:
        self.launch()
        if not self.wait_listening(_START_TIMEOUT_S):
            self.stop()
            raise CuaDriverError("CuaDriver.app did not start its desktop service in time")

    def listening(self) -> bool:
        try:
            probe = self._run([self.binary, "status", "--socket", self.socket_path],
                              capture_output=True, text=True, timeout=3, env=self.env)
        except (OSError, subprocess.SubprocessError):
            return False
        return probe.returncode == 0

    def stop(self) -> None:
        """Retire this daemon only; idempotent and safe from another thread."""

        self._stop_requested = True
        if self._launched:
            self._launched = False
            for argv in (
                [self.binary, "stop", "--socket", self.socket_path],
                # A daemon still waiting at its permission gate has no socket
                # to receive stop; its unique socket path names its process.
                ["/usr/bin/pkill", "-f", "--", self.socket_path],
            ):
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    self._run(argv, capture_output=True, timeout=5, env=self.env)
        with contextlib.suppress(OSError):
            os.remove(self.socket_path)


class MacPermissionRequest:
    """Ask macOS for CuaDriver's grants once, in the background."""

    def __init__(self, binary: str, *, env: Mapping[str, str],
                 daemon_factory: Callable[..., MacDriverDaemon] = MacDriverDaemon) -> None:
        self._binary = binary
        self._env = dict(env)
        self._factory = daemon_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # The gate daemon this request launched, owned until it is stopped.
        self._daemon: MacDriverDaemon | None = None
        self._cancel = threading.Event()
        self.granted = threading.Event()
        # The last grant status the driver reported (None: not known yet).
        self.last_status: dict[str, bool | None] = {"accessibility": None, "screen_recording": None}

    def active(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> bool:
        """Start one request; False when one is already waiting on the user."""

        with self._lock:
            if self.active():
                return False
            self.granted.clear()
            self._cancel.clear()
            self._thread = threading.Thread(target=self._wait, name="cua-driver-grant", daemon=True)
            self._thread.start()
            return True

    def cancel(self, timeout_s: float = 5.0) -> None:
        """Stop a pending request and its gate daemon (host shutdown).

        Only the daemon this request launched is stopped. Repeated calls,
        and calls with nothing pending, do nothing.
        """

        self._cancel.set()
        with self._lock:
            daemon, thread = self._daemon, self._thread
        if daemon is not None:
            daemon.stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)

    def _wait(self) -> None:
        daemon = self._factory(self._binary, env=self._env, gate=True)
        with self._lock:
            self._daemon = daemon
        try:
            if self._cancel.is_set():
                return
            daemon.launch()
            # A cancel that arrived while launching finds the daemon here.
            if not self._cancel.is_set() and daemon.wait_listening(_GRANT_TIMEOUT_S) \
                    and not self._cancel.is_set():
                # The gate opens its socket only once both grants are given.
                self.last_status = {"accessibility": True, "screen_recording": True}
                self.granted.set()
        except CuaDriverError:
            pass
        finally:
            daemon.stop()
            with self._lock:
                if self._daemon is daemon:
                    self._daemon = None


def _grant_status(client: Any) -> dict[str, bool | None]:
    """The driver's read-only grant status; None where it did not say."""

    try:
        status = client.call_tool("check_permissions", {"prompt": False})
    except CuaDriverError:
        status = {}
    status = status if isinstance(status, Mapping) else {}
    return {
        key: status[key] if isinstance(status.get(key), bool) else None
        for key in ("accessibility", "screen_recording")
    }


class MacDriverClient:
    """The private daemon plus the stdio MCP proxy connected to it."""

    def __init__(
        self,
        command: list[str],
        *,
        env: Mapping[str, str] | None = None,
        daemon_factory: Callable[..., MacDriverDaemon] = MacDriverDaemon,
        client_factory: Callable[..., Any] = CuaDriverClient,
        permissions: MacPermissionRequest | None = None,
    ) -> None:
        if not command or not command[0]:
            raise CuaDriverError("cua-driver command is empty")
        self.binary = command[0]
        self.env = cua_driver_env() if env is None else dict(env)
        self._daemon_factory = daemon_factory
        self._client_factory = client_factory
        self.permissions = permissions or MacPermissionRequest(
            self.binary, env=self.env, daemon_factory=daemon_factory,
        )
        self.daemon: MacDriverDaemon | None = None
        self._client: Any = None
        self._dead = False
        self.server_info: dict[str, Any] = {}

    def open(self) -> None:
        if self.permissions.active():
            raise CuaDriverError("CuaDriver is still waiting for its macOS permissions. " + PERMISSION_HINT)
        daemon = self._daemon_factory(self.binary, env=self.env)
        daemon.start()
        client: Any = None
        try:
            client = self._client_factory(daemon.proxy_command(), env=self.env)
            client.open()
            grants = _grant_status(client)
        except Exception:
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
            daemon.stop()
            raise
        self.permissions.last_status = grants
        # Only a reported missing grant blocks; an unknown status never does.
        if False in grants.values():
            client.close()
            daemon.stop()
            self.permissions.start()
            raise CuaDriverError("VARIANT-1 asked macOS for CuaDriver's permissions. " + PERMISSION_HINT)
        self.daemon, self._client, self._dead = daemon, client, False
        self.server_info = dict(getattr(client, "server_info", {}) or {})

    def alive(self) -> bool:
        client = self._client
        return client is not None and not self._dead and bool(client.alive())

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        client = self._client
        if client is None:
            raise CuaDriverError("desktop driver is not running")
        try:
            return client.call_tool(name, arguments)
        except CuaDriverError as exc:
            daemon = self.daemon
            if daemon is not None and not daemon.listening():
                # The proxy outlived its daemon; the host starts a new pair.
                self._dead = True
            text = str(exc).lower()
            if "accessibility" in text or "screen recording" in text or "not granted" in text:
                raise CuaDriverError(f"{exc}. {PERMISSION_HINT}") from exc
            raise

    def close(self) -> None:
        client, self._client = self._client, None
        daemon, self.daemon = self.daemon, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
        if daemon is not None:
            daemon.stop()


__all__ = [
    "BUNDLE_ID",
    "MacDriverClient",
    "MacDriverDaemon",
    "MacPermissionRequest",
    "PERMISSION_HINT",
    "TEAM_IDS",
    "app_bundle_for",
    "verify_app_signature",
]
