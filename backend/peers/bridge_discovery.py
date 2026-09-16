"""Stable per-profile discovery for MCP clients; never choose the newest app."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlparse

import psutil


class BridgeUnavailable(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def discovery_directory():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    return base / "VARIANT-1" / "PeerBridge" / "services"


def profile_identity(data_dir):
    path = os.path.normcase(str(Path(data_dir).resolve()))
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]


def process_matches(pid, started_at):
    try:
        process = psutil.Process(int(pid))
        return process.is_running() and abs(process.create_time() - float(started_at)) < .01
    except (psutil.Error, ValueError, TypeError, OverflowError):
        return False


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PublishedBridge:
    def __init__(self, *, data_dir, url, token, directory=None):
        self.profile_id = profile_identity(data_dir)
        self.instance_id = secrets.token_hex(16)
        self.path = Path(directory or discovery_directory()) / self.profile_id / (self.instance_id + ".json")
        self.record = {"schema": "variant1.peer-service.v1", "profile_id": self.profile_id,
            "instance_id": self.instance_id, "pid": os.getpid(), "process_started_at": psutil.Process().create_time(),
            "url": str(url).rstrip("/") + "/peers/bridge", "token": token}
        atomic_json(self.path, self.record)

    def close(self):
        # The instance owns only its own rendezvous file.
        self.path.unlink(missing_ok=True)


def resolve_bridge(profile_id, directory=None):
    if not isinstance(profile_id, str) or len(profile_id) != 32 or any(c not in "0123456789abcdef" for c in profile_id):
        raise BridgeUnavailable("profile_invalid", "The peer bridge profile is invalid.")
    rows = []
    for path in (Path(directory or discovery_directory()) / profile_id).glob("*.json"):
        try:
            row = json.loads(path.read_text("utf-8"))
            url = urlparse(row["url"])
            if row.get("schema") != "variant1.peer-service.v1" or row.get("profile_id") != profile_id:
                continue
            if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path != "/peers/bridge":
                continue
            if process_matches(row["pid"], row["process_started_at"]):
                rows.append(row)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not rows:
        raise BridgeUnavailable("service_offline", "VARIANT-1's peer service is offline. The peer tools remain installed.")
    if len(rows) != 1:
        raise BridgeUnavailable("service_ambiguous", "More than one VARIANT-1 instance owns this profile. Choose one instance before sending.")
    return rows[0]
