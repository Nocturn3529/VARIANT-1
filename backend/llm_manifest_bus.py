"""Out-of-band model-request-manifest store + async publisher.

Owns the bounded receipt window, sink queue, and usage-patch republish path so
``LLMRouter`` stays a thin facade over inference dispatch. Never mutates
provider requests — observability only.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections import deque


class ModelRequestManifestBus:
    """Bounded in-memory receipt window + non-blocking publish queue."""

    def __init__(self, *, maxlen: int = 32, queue_maxsize: int = 64):
        self._items: deque = deque(maxlen=maxlen)
        self._queue_maxsize = queue_maxsize
        self._sink = None
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self.dropped_events = 0
        self.publish_failures = 0
        # The receipt window is intentionally small, but usage is cumulative
        # for the lifetime of this router. Keeping totals separately prevents a
        # long agent run from losing its early Responses cache/reasoning usage
        # when the 33rd structural manifest arrives.
        self._usage_manifest_ids: set[str] = set()
        self._usage_manifest_order: deque[str] = deque(maxlen=8_192)
        self._usage_totals = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "token_volume": 0,
            "prompt_token_volume": 0,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
            "reasoning_tokens": 0,
            "cache_write_input_tokens": 0,
            "tool_prompt_tokens": 0,
            "provider_reported_calls": 0,
            "estimated_calls": 0,
        }

    def set_sink(self, sink) -> None:
        """Receive privacy-safe, post-adapter request receipts."""
        self._sink = sink

    def snapshot(self, *, manifest_id: str = "") -> dict:
        """Return the bounded in-memory receipt window; no prompt content."""
        items = list(self._items)
        wanted = str(manifest_id or "").strip()
        if wanted:
            items = [
                row for row in items
                if str((row or {}).get("manifest_id") or "") == wanted
            ]
        usage = copy.deepcopy(self._usage_totals)
        prompt_volume = int(usage.get("prompt_token_volume") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        usage["cache_share"] = (
            round(cached / prompt_volume, 8) if prompt_volume > 0 else 0.0
        )
        return {
            "type": "model:request_manifests",
            "items": copy.deepcopy(items),
            "usage_scope": "router_lifetime",
            "usage_totals": usage,
            "dropped_events": self.dropped_events,
            "publish_failures": self.publish_failures,
            "filter_manifest_id": wanted or None,
        }

    async def record(self, manifest: dict) -> None:
        """Store and enqueue one receipt without delaying the provider request."""
        if not isinstance(manifest, dict):
            return
        stored = copy.deepcopy(manifest)
        self._items.append(stored)
        route = stored.get("route") or {}
        messages = (stored.get("messages") or {}).get("rendered") or {}
        tools = stored.get("tools") or {}
        images = stored.get("images") or {}
        protocol = stored.get("tool_protocol") or {}
        print(
            f"[model] receipt id={stored.get('manifest_id', '')} "
            f"attempt={stored.get('attempt', 0)} "
            f"provider={route.get('provider', '')} "
            f"adapter={route.get('adapter', '')} "
            f"messages={messages.get('count', 0)} "
            f"tools={tools.get('rendered_count', 0)} "
            f"images={images.get('rendered_count', 0)} "
            f"protocol={'valid' if protocol.get('valid', True) else 'invalid'}",
            flush=True,
        )
        if self._sink is None:
            return
        self.enqueue(stored)

    def enqueue(self, manifest: dict) -> None:
        """Queue one full receipt snapshot without blocking the caller."""
        if self._sink is None:
            return
        stored = copy.deepcopy(manifest)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = self._task
        if task is None or task.done() or task.get_loop() is not loop:
            queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
            self._queue = queue
            self._task = loop.create_task(
                self._publish_loop(queue),
                name="model-request-manifest-publisher",
            )
        queue = self._queue
        try:
            queue.put_nowait(copy.deepcopy(stored))
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self.dropped_events += 1
            try:
                queue.put_nowait(copy.deepcopy(stored))
            except asyncio.QueueFull:
                self.dropped_events += 1

    @staticmethod
    def manifest_id_from_ref(manifest_ref) -> str:
        if isinstance(manifest_ref, str):
            return manifest_ref[:160]
        if isinstance(manifest_ref, dict):
            return str(manifest_ref.get("manifest_id") or "")[:160]
        return str(getattr(manifest_ref, "manifest_id", "") or "")[:160]

    def patch_usage(self, manifest_ref, normalized_usage: dict) -> None:
        """Patch and republish the exact bounded request receipt, fail-open."""
        manifest_id = self.manifest_id_from_ref(manifest_ref)
        if not manifest_id or not isinstance(normalized_usage, dict):
            return
        if manifest_id not in self._usage_manifest_ids:
            if (
                self._usage_manifest_order.maxlen
                and len(self._usage_manifest_order)
                >= self._usage_manifest_order.maxlen
            ):
                expired = self._usage_manifest_order[0]
                self._usage_manifest_ids.discard(expired)
            self._usage_manifest_order.append(manifest_id)
            self._usage_manifest_ids.add(manifest_id)
            totals = self._usage_totals
            totals["calls"] += 1
            for field in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "token_volume",
                "prompt_token_volume",
                "cached_input_tokens",
                "uncached_input_tokens",
                "reasoning_tokens",
                "cache_write_input_tokens",
                "tool_prompt_tokens",
            ):
                try:
                    value = max(0, int(normalized_usage.get(field) or 0))
                except (TypeError, ValueError, OverflowError):
                    value = 0
                totals[field] += value
            if bool(normalized_usage.get("provider_reported")):
                totals["provider_reported_calls"] += 1
            if bool(normalized_usage.get("estimated")):
                totals["estimated_calls"] += 1
        for index in range(len(self._items) - 1, -1, -1):
            current = self._items[index]
            if str((current or {}).get("manifest_id") or "") != manifest_id:
                continue
            updated = copy.deepcopy(current)
            updated["usage"] = copy.deepcopy(normalized_usage)
            provenance = updated.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
                updated["provenance"] = provenance
            provenance["usage_available"] = True
            self._items[index] = updated
            try:
                from observability.trace_events import record_model_usage

                record_model_usage(updated)
            except Exception:
                pass
            self.enqueue(updated)
            return

    def patch_response_metadata(self, manifest_ref, metadata: dict) -> None:
        """Patch allowlisted provider-returned identity fields, fail-open.

        Provider streams may expose a concrete model ID, model revision, or
        system fingerprint only after the request receipt has been emitted.
        This method correlates those values by manifest ID without retaining
        response text or arbitrary provider payload fields.
        """
        manifest_id = self.manifest_id_from_ref(manifest_ref)
        if not manifest_id or not isinstance(metadata, dict):
            return
        allowed = {
            key: str(metadata.get(key) or "").strip()[:300]
            for key in (
                "provider_returned_model_id",
                "model_revision",
                "system_fingerprint",
            )
            if str(metadata.get(key) or "").strip()
        }
        if not allowed:
            return
        for index in range(len(self._items) - 1, -1, -1):
            current = self._items[index]
            if str((current or {}).get("manifest_id") or "") != manifest_id:
                continue
            updated = copy.deepcopy(current)
            route = updated.get("route")
            if not isinstance(route, dict):
                route = {}
                updated["route"] = route
            route.update(allowed)
            self._items[index] = updated
            self.enqueue(updated)
            return

    async def stop(self) -> None:
        """Cancel the publisher task (engine shutdown)."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._queue = None

    async def _publish_loop(self, queue: asyncio.Queue) -> None:
        """Publish receipts out-of-band; slow sockets cannot delay inference."""
        while True:
            manifest = await queue.get()
            try:
                sink = self._sink
                if sink is not None:
                    result = sink(copy.deepcopy(manifest))
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.publish_failures += 1
            finally:
                queue.task_done()
