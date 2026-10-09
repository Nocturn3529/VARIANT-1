"""macOS CI: the pinned CuaDriver.app starts through LaunchServices and serves MCP.

Run after scripts/install-cua-driver.js. CI runners hold no Accessibility or
Screen Recording grants, so this checks the launch path VARIANT-1 depends on:
trycua's signature, the private daemon on its own socket, the MCP proxy
handshake at the pinned version, the tool surface, the permission status the
backend reads, and a clean stop.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "backend")

from desktop_fabric.cua_client import (  # noqa: E402
    CuaDriverClient,
    cua_driver_env,
    pinned_cua_driver_version,
    resolve_cua_driver_command,
)
from desktop_fabric.cua_macos import MacDriverDaemon, app_bundle_for  # noqa: E402

REQUIRED_TOOLS = ("list_windows", "get_window_state", "click", "check_permissions",
                  "start_session", "end_session")


def main() -> None:
    command = resolve_cua_driver_command()
    if not command or not app_bundle_for(command[0]):
        raise SystemExit(f"CuaDriver.app was not resolved: {command}")
    print("cua_driver_command", command)
    env = cua_driver_env()
    daemon = MacDriverDaemon(command[0], env=env)
    daemon.start()
    client = None
    try:
        client = CuaDriverClient(daemon.proxy_command(), env=env)
        client.open()
        version = str(client.server_info.get("version") or "")
        pinned = pinned_cua_driver_version(command[0])
        if version != pinned:
            raise SystemExit(f"driver reports {version!r}, pinned {pinned!r}")
        names = set(client.list_tools())
        missing = [name for name in REQUIRED_TOOLS if name not in names]
        if missing:
            raise SystemExit(f"driver lacks tools {missing}; has {sorted(names)}")
        permissions = client.call_tool("check_permissions", {"prompt": False})
        status = {key: permissions.get(key) for key in ("accessibility", "screen_recording")}
        if not all(isinstance(value, bool) for value in status.values()):
            raise SystemExit(f"check_permissions answered without its booleans: {permissions!r}")
        print("macos_desktop_ok", version, len(names), status)
    finally:
        if client is not None:
            client.close()
        daemon.stop()
    if daemon.listening():
        raise SystemExit("the private daemon still listens after stop")


if __name__ == "__main__":
    main()
