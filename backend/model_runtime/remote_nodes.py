"""Persistent remote inference-node inventory and controller probing."""

from __future__ import annotations

import os
import re
import time
import uuid
from urllib.parse import urlparse

import httpx

from model_runtime.platform_store import AtomicJsonStore


def _base_url(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("node URL must be an http:// or https:// address")
    if text.endswith("/v1"):
        text = text[:-3].rstrip("/")
    return text


def _node_id(value: str = "") -> str:
    clean = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return (clean[:50] or "node") + "-" + uuid.uuid4().hex[:8]


class RemoteNodeManager:
    """Saved local/LAN controllers with optional lifecycle delegation."""

    def __init__(self, data_dir: str, *, event_sink=None) -> None:
        path = os.path.join(data_dir, "data", "inference", "remote_nodes.json")
        self.store = AtomicJsonStore(path, {"version": 1, "items": []})
        loaded = self.store.load()
        rows = loaded.get("items") if isinstance(loaded, dict) else []
        self.nodes: list[dict] = [row for row in (rows or []) if isinstance(row, dict)]
        self.probes: dict[str, dict] = {}
        self.event_sink = event_sink

    def list(self) -> dict:
        return {
            "type": "inference:nodes",
            "items": [
                {**row, "probe": self.probes.get(str(row.get("id") or ""), {})}
                for row in self.nodes
            ],
        }

    def get(self, node_id: str) -> dict | None:
        return next((row for row in self.nodes if row.get("id") == node_id), None)

    def save(self, value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("node must be an object")
        node_id = str(value.get("id") or "").strip()
        existing = self.get(node_id) if node_id else None
        base = _base_url(str(value.get("base_url") or (existing or {}).get("base_url") or ""))
        if not base:
            raise ValueError("node inference URL is required")
        management_raw = str(value.get("management_url") or (existing or {}).get("management_url") or base)
        management = _base_url(management_raw)
        name = str(value.get("name") or (existing or {}).get("name") or urlparse(base).hostname or "Remote node").strip()[:100]
        row = {
            "id": node_id or _node_id(name),
            "name": name,
            "base_url": base,
            "management_url": management,
            "api_key_env": str(value.get("api_key_env") or (existing or {}).get("api_key_env") or "").strip()[:120],
            "ssh_target": str(value.get("ssh_target") or (existing or {}).get("ssh_target") or "").strip()[:240],
            "model_directory": str(value.get("model_directory") or (existing or {}).get("model_directory") or "").strip()[:500],
            "enabled": bool(value.get("enabled", (existing or {}).get("enabled", True))),
            "created_at": float((existing or {}).get("created_at") or time.time()),
            "updated_at": time.time(),
        }
        if existing:
            candidate = [row if item.get("id") == row["id"] else item for item in self.nodes]
        else:
            candidate = [*self.nodes, row]
        self.store.save({"version": 1, "items": candidate})
        self.nodes = candidate
        self._event("info", f"Remote inference node saved: {name}", {"node_id": row["id"]})
        return row

    def remove(self, node_id: str) -> bool:
        before = len(self.nodes)
        candidate = [row for row in self.nodes if row.get("id") != node_id]
        changed = len(candidate) != before
        if changed:
            self.store.save({"version": 1, "items": candidate})
            self.nodes = candidate
            self.probes.pop(node_id, None)
            self._event("info", "Remote inference node removed", {"node_id": node_id})
        return changed

    def _headers(self, node: dict) -> dict:
        env = str(node.get("api_key_env") or "")
        token = str(os.getenv(env) or "") if env else ""
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def probe(self, node_id: str) -> dict:
        node = self.get(node_id)
        if not node:
            raise ValueError("remote inference node not found")
        started = time.perf_counter()
        result = {
            "node_id": node_id,
            "name": node.get("name", "Remote node"),
            "ready": False,
            "models": [],
            "health": {},
            "status": {},
            "gpus": [],
            "runtime_targets": [],
            "recipes": [],
            "capabilities": [],
            "latency_ms": 0.0,
            "checked_at": time.time(),
            "error": "",
        }
        headers = self._headers(node)
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False, headers=headers) as client:
                models_response = await client.get(f"{node['base_url']}/v1/models")
                models_response.raise_for_status()
                models_body = models_response.json()
                rows = models_body.get("data") if isinstance(models_body, dict) else []
                result["models"] = [
                    str(row.get("id")) if isinstance(row, dict) else str(row)
                    for row in (rows or []) if row
                ]
                result["capabilities"].append("openai")
                result["ready"] = True
                management = node.get("management_url") or node["base_url"]
                for path, key, capability in (
                    ("/health", "health", "health"),
                    ("/status", "status", "status"),
                    ("/gpus", "gpus", "gpu_metrics"),
                    ("/runtime/targets", "runtime_targets", "runtime_targets"),
                    ("/recipes", "recipes", "recipes"),
                ):
                    try:
                        response = await client.get(f"{management}{path}")
                        if response.status_code != 200:
                            continue
                        body = response.json()
                        if key in {"gpus", "runtime_targets", "recipes"}:
                            result[key] = body if isinstance(body, list) else body.get("items", body.get("data", [])) if isinstance(body, dict) else []
                        else:
                            result[key] = body if isinstance(body, dict) else {"value": body}
                        result["capabilities"].append(capability)
                    except Exception:
                        continue
        except Exception as exc:
            result["ready"] = False
            result["error"] = str(exc)[:500]
            self._event("error", f"Remote node {node.get('name')} probe failed: {exc}", {"node_id": node_id})
        result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        result["capabilities"] = sorted(set(result["capabilities"]))
        self.probes[node_id] = result
        return result

    async def launch(self, node_id: str, remote_recipe_id: str) -> dict:
        node = self.get(node_id)
        if not node:
            raise ValueError("remote inference node not found")
        if not remote_recipe_id:
            raise ValueError("remote recipe ID is required")
        management = node.get("management_url") or node["base_url"]
        async with httpx.AsyncClient(timeout=30.0, trust_env=False, headers=self._headers(node)) as client:
            response = await client.post(f"{management}/launch/{remote_recipe_id}")
        if response.status_code >= 300:
            raise RuntimeError(f"remote launch failed ({response.status_code}): {response.text[:300]}")
        self._event("info", f"Remote recipe launch requested on {node.get('name')}", {"node_id": node_id, "recipe_id": remote_recipe_id})
        return {"ok": True, "status_code": response.status_code}

    async def evict(self, node_id: str) -> dict:
        node = self.get(node_id)
        if not node:
            raise ValueError("remote inference node not found")
        management = node.get("management_url") or node["base_url"]
        async with httpx.AsyncClient(timeout=20.0, trust_env=False, headers=self._headers(node)) as client:
            response = await client.post(f"{management}/evict")
        if response.status_code >= 300:
            raise RuntimeError(f"remote eviction failed ({response.status_code}): {response.text[:300]}")
        self._event("info", f"Remote runtime evicted on {node.get('name')}", {"node_id": node_id})
        return {"ok": True, "status_code": response.status_code}

    def _event(self, level: str, message: str, metadata: dict | None = None) -> None:
        if self.event_sink:
            self.event_sink("remote", level, message, metadata)


__all__ = ["RemoteNodeManager"]
