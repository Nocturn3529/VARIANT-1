"""Strict, deterministic contracts for executable VARIANT-1 extension packages."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from core_invariants import canonical_json_bytes as _stable
from file_paths import is_reparse_or_link

PLUGIN_MANIFEST = "variant1.plugin.json"
CONTRIBUTION_KINDS = frozenset({
    "skills", "capabilities", "commands", "panels", "artifact_renderers",
    "research_providers", "automation_triggers",
    "automation_actions", "model_providers", "kernel_serializers",
    "settings_sections", "deck_destinations", "context_tabs",
    "composer_actions", "artifact_viewers", "connection_types",
    "messaging_adapters",
})
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")
_IGNORED = frozenset({".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "node_modules", "dist", "build"})


class ExtensionManifestError(ValueError):
    pass


def _relative_path(value: Any, field: str) -> str:
    text = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ExtensionManifestError(f"{field} must be a contained relative path")
    return path.as_posix()


@dataclass(frozen=True)
class ExtensionManifest:
    package_id: str
    name: str
    version: str
    compatibility: Mapping[str, Any]
    entrypoints: Mapping[str, Any]
    contributions: Mapping[str, tuple[Mapping[str, Any], ...]]
    dependencies: Mapping[str, Any]
    permissions: tuple[str, ...]
    raw: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(_stable(self.raw))


def parse_extension_manifest(value: Mapping[str, Any]) -> ExtensionManifest:
    raw = dict(value)
    if int(raw.get("schema_version") or 0) != 2:
        raise ExtensionManifestError("schema_version must be 2")
    package_id = str(raw.get("id") or "").strip().lower()
    if not _ID.fullmatch(package_id):
        raise ExtensionManifestError("id must be a lowercase reverse-domain identifier")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 200:
        raise ExtensionManifestError("name is required and must be at most 200 characters")
    version = str(raw.get("version") or "").strip()
    if not _VERSION.fullmatch(version):
        raise ExtensionManifestError("version must be SemVer major.minor.patch")
    compatibility = dict(raw.get("compatibility") or {})
    entrypoints = dict(raw.get("entrypoints") or {})
    python = entrypoints.get("python")
    if python is not None:
        if not isinstance(python, Mapping) or not str(python.get("module") or "").strip():
            raise ExtensionManifestError("entrypoints.python.module is required")
        if str(python.get("environment") or "isolated") != "isolated":
            raise ExtensionManifestError("executable Python entrypoints must use an isolated environment")
    servers = entrypoints.get("mcp_servers") or []
    if not isinstance(servers, list):
        raise ExtensionManifestError("entrypoints.mcp_servers must be a list")
    for index, server in enumerate(servers):
        if not isinstance(server, Mapping) or not _ID.fullmatch(str(server.get("id") or "")):
            raise ExtensionManifestError(f"mcp_servers[{index}].id is invalid")
        transport = str(server.get("transport") or "stdio")
        if transport not in {"stdio", "streamable_http", "sse"}:
            raise ExtensionManifestError(f"mcp_servers[{index}].transport is unsupported")
        if transport == "stdio" and not isinstance(server.get("command"), list):
            raise ExtensionManifestError(f"mcp_servers[{index}].command must be an argument array")
        if transport != "stdio" and not str(server.get("url") or "").startswith(("http://", "https://")):
            raise ExtensionManifestError(f"mcp_servers[{index}].url must be HTTP(S)")
    raw_contributes = raw.get("contributes") or {}
    if not isinstance(raw_contributes, Mapping):
        raise ExtensionManifestError("contributes must be an object")
    unknown = set(raw_contributes).difference(CONTRIBUTION_KINDS)
    if unknown:
        raise ExtensionManifestError("unknown contribution kinds: " + ", ".join(sorted(unknown)))
    contributions: dict[str, tuple[Mapping[str, Any], ...]] = {}
    identities: set[tuple[str, str]] = set()
    for kind, rows in raw_contributes.items():
        if not isinstance(rows, list):
            raise ExtensionManifestError(f"contributes.{kind} must be a list")
        clean = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ExtensionManifestError(f"contributes.{kind}[{index}] must be an object")
            item = dict(row)
            identifier = str(item.get("id") or "").strip()
            if not _ID.fullmatch(identifier):
                raise ExtensionManifestError(f"contributes.{kind}[{index}].id is invalid")
            if (kind, identifier) in identities:
                raise ExtensionManifestError(f"duplicate contribution: {kind}/{identifier}")
            identities.add((kind, identifier))
            if kind in {"capabilities", "artifact_renderers", "research_providers",
                        "automation_triggers", "automation_actions", "model_providers",
                        "kernel_serializers", "messaging_adapters"}:
                handler = str(item.get("handler") or "")
                if ":" not in handler or any(not part.strip() for part in handler.split(":", 1)):
                    raise ExtensionManifestError(f"{kind}.{identifier}.handler must be module:symbol")
            if kind == "capabilities":
                effect = str(item.get("effect_class") or "read")
                if effect not in {"pure", "read", "write", "external_effect"}:
                    raise ExtensionManifestError(f"capabilities.{identifier}.effect_class is invalid")
                if bool(item.get("parallel_safe")) and effect not in {"pure", "read"}:
                    raise ExtensionManifestError(
                        f"capabilities.{identifier} cannot mark an effectful handler parallel-safe"
                    )
            if kind == "messaging_adapters":
                effect = str(item.get("effect_class") or "external_effect")
                if effect != "external_effect":
                    raise ExtensionManifestError(
                        f"messaging_adapters.{identifier}.effect_class must be external_effect"
                    )
                item["effect_class"] = effect
                interval = float(item.get("poll_interval_s") or 2.0)
                if interval < 0.1 or interval > 60:
                    raise ExtensionManifestError(
                        f"messaging_adapters.{identifier}.poll_interval_s is invalid"
                    )
                item["poll_interval_s"] = interval
            for field in ("path", "entry", "input_schema"):
                if item.get(field):
                    item[field] = _relative_path(item[field], f"{kind}.{identifier}.{field}")
            clean.append(item)
        contributions[str(kind)] = tuple(clean)
    dependencies = dict(raw.get("dependencies") or {})
    plugin_dependencies = dependencies.get("plugins") or {}
    if not isinstance(plugin_dependencies, Mapping):
        raise ExtensionManifestError("dependencies.plugins must be an object")
    for dependency_id, requirement in plugin_dependencies.items():
        if not _ID.fullmatch(str(dependency_id)) or not str(requirement).strip():
            raise ExtensionManifestError("plugin dependency identifiers and ranges are required")
    permissions = tuple(dict.fromkeys(str(item) for item in (raw.get("permissions") or []) if str(item)))
    normalized = dict(raw)
    normalized["id"] = package_id
    normalized["name"] = name
    normalized["version"] = version
    return ExtensionManifest(package_id, name, version, compatibility, entrypoints,
                             contributions, dependencies, permissions, normalized)


def load_extension_manifest(root: str | Path) -> ExtensionManifest:
    path = Path(root).resolve() / PLUGIN_MANIFEST
    if not path.is_file():
        raise ExtensionManifestError(f"package requires {PLUGIN_MANIFEST}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ExtensionManifestError(f"invalid {PLUGIN_MANIFEST}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ExtensionManifestError("plugin manifest root must be an object")
    return parse_extension_manifest(value)


def source_manifest(root: str | Path) -> tuple[list[dict[str, Any]], str]:
    original = Path(root).absolute()
    if is_reparse_or_link(str(original)):
        raise ExtensionManifestError("extension source root is a link or reparse point")
    base = original.resolve()
    rows: list[dict[str, Any]] = []
    total = 0
    paths: list[Path] = []
    pending = [base]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            children = sorted(entries, key=lambda item: item.name)
        for child in children:
            if child.name in _IGNORED:
                continue
            path = Path(child.path)
            if is_reparse_or_link(str(path)):
                raise ExtensionManifestError(
                    f"extension source contains a link or reparse point: "
                    f"{path.relative_to(base).as_posix()}"
                )
            if child.is_dir(follow_symlinks=False):
                pending.append(path)
            elif child.is_file(follow_symlinks=False):
                paths.append(path)
    for path in sorted(paths, key=lambda p: p.relative_to(base).as_posix()):
        relative = path.relative_to(base)
        data = path.read_bytes(); total += len(data)
        if total > 64 * 1024 * 1024:
            raise ExtensionManifestError("extension source exceeds 64 MiB")
        rows.append({"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    if not rows:
        raise ExtensionManifestError("extension source is empty")
    digest = hashlib.sha256(_stable(rows)).hexdigest()
    return rows, digest


__all__ = ["CONTRIBUTION_KINDS", "ExtensionManifest", "ExtensionManifestError",
           "PLUGIN_MANIFEST", "load_extension_manifest", "parse_extension_manifest", "source_manifest"]
