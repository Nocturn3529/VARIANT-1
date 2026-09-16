"""Composition-neutral executable extension runtime factory."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .mcp_v2 import McpV2Service
from .manifests_v2 import load_extension_manifest, source_manifest
from .packages_v2 import ExtensionPackageService
from .skill_catalog import SkillCatalogService
from .worker_host import PluginWorkerHost


class ExtensionV2Runtime:
    def __init__(
        self,
        packages: ExtensionPackageService,
        skills: SkillCatalogService,
        mcp: McpV2Service,
        workers: PluginWorkerHost,
        plugin_sources: tuple[str, ...] = (),
    ) -> None:
        self.packages = packages
        self.skills = skills
        self.mcp = mcp
        self.workers = workers
        self.plugin_sources = tuple(str(path) for path in plugin_sources if str(path))
        self.scan_errors: dict[str, str] = {}
        self.started = False
        self.last_reconnect: dict[str, bool] = {}

    async def start(self) -> dict[str, Any]:
        if not self.started:
            await asyncio.to_thread(self.rescan)
            await self.workers.start()
            self.last_reconnect = await self.mcp.reconnect_all()
            self.started = True
        return self.state()

    async def shutdown(self) -> None:
        try:
            await self.mcp.disconnect_all()
        finally:
            await self.workers.shutdown()
            self.started = False

    def state(self) -> dict[str, Any]:
        configured = [
            {key: value for key, value in row.items() if key != "spec"}
            for row in self.mcp.configured()
        ]
        return {
            "schema": "variant1.extension-runtime.v2",
            "started": self.started,
            "installed": self.plugins(limit=500),
            "skills": self.skills.list(),
            "mcp": configured,
            "workers": self.workers.state(),
            "last_reconnect": dict(self.last_reconnect),
            "limitations": {
                "oauth_pkce": "not_implemented",
                "remote_marketplace": "not_implemented",
                "worker_isolation": "process_and_import_path_isolation_not_os_sandbox",
            },
        }

    def rescan(self) -> dict[str, Any]:
        """Install or update manifest-backed plugins found in configured roots."""

        discovered = installed = updated = unchanged = 0
        errors: dict[str, str] = {}
        known = {row["package_id"]: row for row in self.packages.list_packages(limit=5000)}
        seen_roots: set[str] = set()
        for raw_root in self.plugin_sources:
            root = Path(raw_root).resolve()
            key = str(root).casefold()
            if key in seen_roots:
                continue
            seen_roots.add(key)
            if not root.is_dir():
                continue
            for source in sorted(
                (item for item in root.iterdir() if item.is_dir()),
                key=lambda item: item.name.casefold(),
            ):
                if not (source / "variant1.plugin.json").is_file():
                    continue
                discovered += 1
                try:
                    manifest = load_extension_manifest(source)
                    _files, digest = source_manifest(source)
                    prior_latest = known.get(manifest.package_id)
                    try:
                        same_version = self.packages.inspect(
                            manifest.package_id, version=manifest.version,
                        )
                    except LookupError:
                        same_version = None
                    if same_version is not None:
                        if str(same_version["package_digest"]) != digest:
                            raise ValueError(
                                "this plugin version changed; increment its version before rescanning"
                            )
                        unchanged += 1
                        continue
                    activate = bool(prior_latest["active"]) if prior_latest else True
                    self.packages.install(str(source), activate=activate)
                    if prior_latest:
                        updated += 1
                    else:
                        installed += 1
                    matches = self.packages.list_packages(
                        query=manifest.package_id, limit=20,
                    )
                    if matches:
                        known[manifest.package_id] = next(
                            (row for row in matches if row["package_id"] == manifest.package_id),
                            matches[0],
                        )
                except Exception as exc:
                    errors[str(source)] = str(exc) or type(exc).__name__
        self.scan_errors = errors
        return {
            "discovered": discovered,
            "installed": installed,
            "updated": updated,
            "unchanged": unchanged,
            "errors": [
                {"source_path": path, "error": message}
                for path, message in sorted(errors.items())
            ],
        }

    def plugins(self, query: str = "", *, limit: int = 500) -> list[dict[str, Any]]:
        """Return the compact Settings projection, including disabled packages."""

        output: list[dict[str, Any]] = []
        for row in self.packages.list_packages(query=query, limit=limit):
            detail = self.packages.inspect(
                str(row["package_id"]), digest=str(row["package_digest"]),
            )
            manifest = dict(detail.get("manifest") or {})
            kinds = sorted({
                str(item.get("kind") or "")
                for item in detail.get("contributions") or ()
                if str(item.get("kind") or "")
            })
            output.append({
                **row,
                "description": str(manifest.get("description") or ""),
                "contribution_count": len(detail.get("contributions") or ()),
                "contribution_kinds": kinds,
                "status": str(detail.get("status") or "ready"),
            })
        needle = str(query or "").casefold()
        for path, message in sorted(self.scan_errors.items()):
            name = Path(path).name
            if needle and needle not in name.casefold() and needle not in message.casefold():
                continue
            output.append({
                "package_id": f"invalid:{name}",
                "name": name,
                "version": "",
                "package_digest": "",
                "catalog_revision": "",
                "active": False,
                "description": "Plugin could not be loaded.",
                "contribution_count": 0,
                "contribution_kinds": [],
                "status": "error",
                "error": message,
                "source_path": path,
            })
        return output[:max(1, min(int(limit), 5000))]

    def set_enabled(self, package_id: str, enabled: bool) -> dict[str, Any]:
        if enabled:
            detail = self.packages.activate(str(package_id))
            return {"package_id": str(package_id), "active": True, "detail": detail}
        changed = self.packages.deactivate(str(package_id))
        return {"package_id": str(package_id), "active": False, "changed": changed}


def create_extension_v2_runtime(
    data_dir: str,
    *,
    variant1_version: str = "0.1.0",
    environment_builder=None,
    mcp_opener=None,
    mcp_max_concurrency: int = 16,
    mcp_deadline_s: float = 120.0,
    worker_max_concurrency: int = 8,
    worker_process_concurrency: int = 4,
    worker_deadline_s: float = 30.0,
    worker_startup_deadline_s: float = 10.0,
    worker_max_read_retries: int = 1,
    worker_command_builder=None,
    plugin_sources=(),
) -> ExtensionV2Runtime:
    root = Path(data_dir).resolve() / "extensions"
    database = str(root / "extensions.sqlite3")
    packages = ExtensionPackageService(
        database, str(root), variant1_version=variant1_version,
        environment_builder=environment_builder,
    )
    packages.retire_legacy_catalog()
    skills = SkillCatalogService(packages)
    mcp = McpV2Service(
        opener=mcp_opener, max_concurrency=mcp_max_concurrency,
        deadline_s=mcp_deadline_s, database_path=database,
    )
    workers = PluginWorkerHost(
        packages,
        database,
        maximum_concurrency=worker_max_concurrency,
        worker_concurrency=worker_process_concurrency,
        default_deadline_s=worker_deadline_s,
        startup_deadline_s=worker_startup_deadline_s,
        max_read_retries=worker_max_read_retries,
        command_builder=worker_command_builder,
    )
    return ExtensionV2Runtime(
        packages, skills, mcp, workers,
        plugin_sources=tuple(str(path) for path in plugin_sources if str(path)),
    )


__all__ = ["ExtensionV2Runtime", "create_extension_v2_runtime"]
