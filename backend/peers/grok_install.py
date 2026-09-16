"""Idempotent installation of the shared MCP client into Grok's user plugin scope."""
from __future__ import annotations

import json
import asyncio
import os
from pathlib import Path
import shutil
import sys
import threading

from .bridge_discovery import atomic_json, discovery_directory, profile_identity
from . import install_lock


class GrokPeerError(RuntimeError):
    def __init__(self, code, message, *, commit_state="unknown"):
        super().__init__(message)
        self.code, self.commit_state = code, commit_state


def grok_executable():
    found = shutil.which("grok.exe" if os.name == "nt" else "grok")
    if found:
        return found
    candidate = Path.home() / ".grok" / "bin" / ("grok.exe" if os.name == "nt" else "grok")
    return str(candidate) if candidate.is_file() else ""


def bridge_command(host):
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command += [str(Path(__file__).resolve().parents[1] / "server.py")]
    publication = getattr(host, "peer_bridge_publication", None)
    directory = publication.path.parent.parent if publication else discovery_directory()
    return command + ["--peer-bridge", "--harness", "grok", "--profile-id", profile_identity(host.data_dir),
                      "--discovery-dir", str(directory)]


def plugin_source():
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok").resolve() / "plugins" / "variant1-peers"


def mcp_definition(host):
    command = bridge_command(host)
    return {"command": command[0], "args": command[1:]}


def installation_profile():
    source = plugin_source()
    try:
        receipt = json.loads((source / ".variant1-installation.json").read_text("utf-8"))
        if receipt.get("profile_id"):
            return str(receipt["profile_id"])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        args = json.loads((source / ".mcp.json").read_text("utf-8"))["mcpServers"]["variant1-peers"].get("args", [])
        index = args.index("--profile-id")
        return str(args[index + 1])
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError):
        return ""


def installed(host):
    try:
        source = plugin_source()
        data = json.loads((source / ".mcp.json").read_text("utf-8"))
        receipt = json.loads((source / ".variant1-installation.json").read_text("utf-8"))
        target = Path(receipt["installed_path"]).resolve()
        target.relative_to((source.parent.parent / "installed-plugins").resolve())
        copied = json.loads((target / ".mcp.json").read_text("utf-8"))
        expected = mcp_definition(host)
        return receipt.get("profile_id") == profile_identity(host.data_dir) and data.get("mcpServers", {}).get("variant1-peers") == expected and copied == data
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


async def install(host, execution, owner, cwd, *, replace_profile_id=""):
    cancelled = threading.Event()
    pending = asyncio.create_task(asyncio.to_thread(install_lock.acquire,
        plugin_source().parent / ".variant1-peer-install.lock", 30, cancelled))
    try:
        lock = await asyncio.shield(pending)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            acquired = await asyncio.shield(pending)
        except (InterruptedError, TimeoutError):
            pass
        else:
            install_lock.release(acquired)
        raise
    try:
        current = installation_profile()
        wanted = profile_identity(host.data_dir)
        if current and current != wanted and replace_profile_id != current:
            raise GrokPeerError("grok_adapter_profile_conflict", "Grok's peer adapter is linked to another VARIANT-1 profile. Use the explicit profile switch action to change future connections.", commit_state="not_committed")
        if replace_profile_id and current != replace_profile_id and current != wanted:
            raise GrokPeerError("grok_adapter_profile_changed", "The adapter profile changed before this switch. Refresh and choose again.", commit_state="not_committed")
        return await _install_locked(host, execution, owner, cwd)
    finally:
        install_lock.release(lock)


async def _install_locked(host, execution, owner, cwd):
    source = plugin_source()
    marker = source / ".variant1-managed"
    if source.exists() and (not marker.is_file() or marker.read_text("utf-8") != "variant1-grok-peer-adapter-v1"):
        raise GrokPeerError("grok_adapter_name_conflict", "The variant1-peers Grok plugin directory is already in use.", commit_state="not_committed")
    source.mkdir(parents=True, exist_ok=True)
    marker.write_text("variant1-grok-peer-adapter-v1", "utf-8")
    atomic_json(source / "plugin.json", {"name": "variant1-peers", "version": "0.3.0",
        "description": "Persistent peer messaging for the current native Grok session, across normal launch and resume."})
    atomic_json(source / ".mcp.json", {"mcpServers": {"variant1-peers": mcp_definition(host)}})

    async def command(arguments):
        result = await execution.run_bounded_process([grok_executable(), *arguments], owner=owner, cwd=cwd,
            timeout=30, max_output_bytes=512000)
        if result.timed_out or result.cancelled or result.process.exit_code != 0 or result.truncated:
            raise GrokPeerError("grok_adapter_install_failed", "Grok adapter setup failed: " + result.stderr.decode("utf-8", errors="replace")[-500:])
        return result.stdout

    def select(rows):
        return [row for row in rows if row.get("name") == "variant1-peers"
            and Path(str(row.get("source") or "")).resolve() == source]

    inventory = json.loads(await command(["plugin", "list", "--json"]))
    if not any(row.get("name") == "variant1-peers" for row in inventory):
        try:
            await command(["plugin", "install", "--trust", str(source)])
        except GrokPeerError:
            # Another setup may have completed the same install concurrently.
            if not select(json.loads(await command(["plugin", "list", "--json"]))):
                raise
        inventory = json.loads(await command(["plugin", "list", "--json"]))
    matches = select(inventory)
    if len(matches) != 1:
        raise GrokPeerError("grok_adapter_registry_invalid", "Grok did not return one matching adapter installation.")
    target = Path(matches[0]["path"]).resolve()
    target.relative_to((source.parent.parent / "installed-plugins").resolve())
    if (target / ".variant1-managed").read_text("utf-8") != "variant1-grok-peer-adapter-v1":
        raise GrokPeerError("grok_adapter_registry_invalid", "The installed adapter is not managed by VARIANT-1.")
    for name in ("plugin.json", ".mcp.json"):
        atomic_json(target / name, json.loads((source / name).read_text("utf-8")))
    # Retire the owned development hook prototype; the shared bridge has no hooks.
    for base in (source, target):
        (base / "hooks" / "hooks.json").unlink(missing_ok=True)
    await command(["plugin", "enable", "variant1-peers"])
    atomic_json(source / ".variant1-installation.json", {"schema": "variant1.peer-adapter-install.v1",
        "profile_id": profile_identity(host.data_dir), "installed_path": str(target)})
    return {"adapter": "grok-peer-bridge", "installed": True, "profile_id": profile_identity(host.data_dir)}
