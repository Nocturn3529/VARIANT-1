"""macOS runs the pinned driver as trycua's signed CuaDriver.app (fakes only)."""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

from desktop_fabric import cua_adapter
from desktop_fabric.cua_client import CuaDriverError, pinned_cua_driver_version
from desktop_fabric.cua_macos import (
    MacDriverClient,
    MacDriverDaemon,
    MacPermissionRequest,
    PERMISSION_HINT,
    app_bundle_for,
    verify_app_signature,
)

_SIGNED = "Executable=/x\nIdentifier=com.trycua.driver\nFormat=app bundle\nTeamIdentifier=4YEC26S9KF\n"


def _app(tmp_path, version="0.28.2"):
    macos = tmp_path / "bin" / "cua-driver" / "CuaDriver.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / "cua-driver"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (tmp_path / "bin" / "cua-driver" / "VERSION").write_text(version + "\n")
    return str(binary)


def _done(code=0, stderr=""):
    return SimpleNamespace(returncode=code, stderr=stderr, stdout="")


class _Runner:
    """Fake subprocess.run: codesign, open, status, stop and pkill."""

    def __init__(self, *, codesign=_SIGNED, ready_after=1, open_code=0):
        self.calls: list[list[str]] = []
        self.codesign = codesign
        self.ready_after = ready_after
        self.open_code = open_code
        self.status_calls = 0

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        name = os.path.basename(argv[0])
        if name == "codesign":
            return _done(0, self.codesign if "-dv" in argv else "")
        if argv[0] == "/usr/bin/open":
            return _done(self.open_code, "LSOpenURLsWithRole() failed" if self.open_code else "")
        if argv[1:2] == ["status"]:
            self.status_calls += 1
            return _done(0 if self.status_calls >= self.ready_after else 1)
        return _done(0)


def test_only_a_binary_inside_cuadriver_app_has_a_bundle(tmp_path):
    binary = _app(tmp_path)
    assert app_bundle_for(binary) == os.path.realpath(str(tmp_path / "bin" / "cua-driver" / "CuaDriver.app"))
    bare = tmp_path / "cua-driver"
    bare.write_text("")
    assert app_bundle_for(str(bare)) is None
    # The pinned version stamp sits next to the bundle.
    assert pinned_cua_driver_version(binary) == "0.28.2"


def test_signature_must_be_trycuas_exact_identity():
    verify_app_signature("/A/CuaDriver.app", run=_Runner())
    for shown in (
        _SIGNED.replace("4YEC26S9KF", "ZZZZZZZZZZ"),
        _SIGNED.replace("com.trycua.driver", "com.trycua.driver.evil"),
        _SIGNED.replace("TeamIdentifier=4YEC26S9KF", "TeamIdentifier=not set"),
    ):
        with pytest.raises(CuaDriverError, match="not trycua"):
            verify_app_signature("/A/CuaDriver.app", run=_Runner(codesign=shown))

    def broken(argv, **_kwargs):
        return _done(1, "code object is not signed at all")
    with pytest.raises(CuaDriverError, match="missing or broken"):
        verify_app_signature("/A/CuaDriver.app", run=broken)


def test_the_daemon_starts_through_launchservices_with_telemetry_off(tmp_path):
    binary = _app(tmp_path)
    runner = _Runner(ready_after=3)
    env = {"CUA_DRIVER_RS_TELEMETRY_ENABLED": "0", "DO_NOT_TRACK": "1", "PATH": "/usr/bin"}
    daemon = MacDriverDaemon(binary, env=env, run=runner, sleep=lambda _s: None,
                             socket_dir=str(tmp_path))
    daemon.start()
    opened = next(call for call in runner.calls if call[0] == "/usr/bin/open")
    assert opened[:3] == ["/usr/bin/open", "-n", "-g"]
    assert opened[opened.index("-a") + 1] == daemon.app
    assert "CUA_DRIVER_RS_TELEMETRY_ENABLED=0" in opened and "DO_NOT_TRACK=1" in opened
    assert "PATH=/usr/bin" not in opened
    assert opened[opened.index("--args") + 1:] == [
        "serve", "--embedded", "--socket", daemon.socket_path, "--no-permissions-gate", "--no-overlay"]
    assert daemon.proxy_command() == [binary, "mcp", "--embedded", "--socket", daemon.socket_path]
    # Before each launch, the bundle's signature is checked.
    assert os.path.basename(runner.calls[0][0]) == "codesign"
    gate = MacDriverDaemon(binary, env=env, gate=True, run=runner, socket_dir=str(tmp_path))
    assert gate.serve_arguments() == ["serve", "--socket", gate.socket_path, "--no-overlay"]


def test_a_daemon_that_never_listens_is_stopped(tmp_path):
    binary = _app(tmp_path)
    runner = _Runner(ready_after=10**9)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    daemon = MacDriverDaemon(binary, env={}, run=runner, sleep=sleep, clock=lambda: now[0],
                             socket_dir=str(tmp_path))
    with pytest.raises(CuaDriverError, match="in time"):
        daemon.start()
    assert [binary, "stop", "--socket", daemon.socket_path] in runner.calls
    assert ["/usr/bin/pkill", "-f", "--", daemon.socket_path] in runner.calls

    failing = MacDriverDaemon(binary, env={}, run=_Runner(open_code=1), socket_dir=str(tmp_path))
    with pytest.raises(CuaDriverError, match="LSOpenURLsWithRole"):
        failing.start()


class _Daemon:
    def __init__(self, binary, *, env, gate=False):
        self.binary, self.env, self.gate = binary, env, gate
        self.socket_path = "/tmp/v1cua-test.sock"
        self.started = self.stopped = False
        self.up = True

    def start(self):
        self.started = True

    def launch(self):
        self.started = True

    def wait_listening(self, _timeout):
        return self.up

    def listening(self):
        return self.up

    def stop(self):
        self.stopped = True

    def proxy_command(self):
        return [self.binary, "mcp", "--embedded", "--socket", self.socket_path]


class _Proxy:
    def __init__(self, argv, *, env, permissions=None, error=None):
        self.argv, self.env = argv, env
        self.permissions = permissions or {"accessibility": True, "screen_recording": True}
        self.error = error
        self.closed = False
        self.server_info = {"version": "0.28.2"}

    def open(self):
        pass

    def alive(self):
        return not self.closed

    def call_tool(self, name, arguments):
        if name == "check_permissions":
            assert arguments == {"prompt": False}
            return dict(self.permissions)
        if self.error:
            raise CuaDriverError(self.error)
        return {"ok": True}

    def close(self):
        self.closed = True


def _client(permissions=None, error=None, request=None):
    made = {}

    def daemon_factory(binary, *, env, gate=False):
        made.setdefault("daemons", []).append(_Daemon(binary, env=env, gate=gate))
        return made["daemons"][-1]

    def proxy_factory(argv, *, env):
        made["proxy"] = _Proxy(argv, env=env, permissions=permissions, error=error)
        return made["proxy"]

    client = MacDriverClient(["/A/CuaDriver.app/Contents/MacOS/cua-driver", "mcp"], env={},
                             daemon_factory=daemon_factory, client_factory=proxy_factory,
                             permissions=request)
    return client, made


def test_the_proxy_talks_only_to_the_private_daemon():
    client, made = _client()
    client.open()
    assert made["proxy"].argv == made["daemons"][0].proxy_command()
    assert client.server_info == {"version": "0.28.2"}
    assert client.call_tool("click", {}) == {"ok": True}
    client.close()
    assert made["proxy"].closed and made["daemons"][0].stopped


def test_missing_grants_ask_macos_once_and_explain_what_to_allow():
    request = MacPermissionRequest("/A/CuaDriver.app/Contents/MacOS/cua-driver", env={})
    started = []
    request.start = lambda: started.append(True) or True  # type: ignore[method-assign]
    client, made = _client(permissions={"accessibility": True, "screen_recording": False},
                           request=request)
    with pytest.raises(CuaDriverError) as raised:
        client.open()
    assert PERMISSION_HINT in str(raised.value)
    assert started == [True]
    assert made["proxy"].closed and made["daemons"][0].stopped
    # An unknown permission status never blocks desktop control.
    client, _made = _client(permissions={"source": "daemon"})
    client.open()


def test_a_waiting_permission_request_is_not_launched_again():
    request = MacPermissionRequest("/A/CuaDriver.app/Contents/MacOS/cua-driver", env={})
    request.active = lambda: True  # type: ignore[method-assign]
    client, made = _client(request=request)
    with pytest.raises(CuaDriverError, match="still waiting"):
        client.open()
    assert "daemons" not in made


def test_the_permission_request_runs_the_gated_app_until_granted():
    daemons = []
    release = threading.Event()

    class Gate(_Daemon):
        def wait_listening(self, _timeout):
            release.wait(5)
            return True

    def factory(binary, *, env, gate=False):
        daemons.append(Gate(binary, env=env, gate=gate))
        return daemons[-1]

    request = MacPermissionRequest("/A/cua-driver", env={}, daemon_factory=factory)
    assert request.start() is True
    assert request.start() is False  # one request at a time
    release.set()
    assert request.granted.wait(5)
    request._thread.join(5)
    assert daemons[0].gate is True and daemons[0].stopped


def test_a_dead_daemon_marks_the_pair_for_restart():
    client, made = _client(error="connection reset")
    client.open()
    made["daemons"][0].up = False
    with pytest.raises(CuaDriverError):
        client.call_tool("click", {})
    assert client.alive() is False
    client, made = _client(error="Accessibility is not granted")
    client.open()
    with pytest.raises(CuaDriverError, match="System Settings"):
        client.call_tool("click", {})


def test_macos_selects_the_app_bundle_or_says_why_not(tmp_path, monkeypatch):
    binary = _app(tmp_path)
    monkeypatch.setattr(cua_adapter, "resolve_cua_driver_command", lambda: [binary, "mcp"])
    adapter = cua_adapter.select_cua_adapter("darwin")
    assert isinstance(adapter, cua_adapter.CuaDesktopAdapter)
    host = adapter.host
    assert host.expected_version == "0.28.2"
    assert isinstance(host._factory([binary, "mcp"]), MacDriverClient)

    bare = tmp_path / "cua-driver"
    bare.write_text("")
    monkeypatch.setattr(cua_adapter, "resolve_cua_driver_command", lambda: [str(bare), "mcp"])
    adapter = cua_adapter.select_cua_adapter("darwin")
    assert "CuaDriver.app" in adapter.reason
