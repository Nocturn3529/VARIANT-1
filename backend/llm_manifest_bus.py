"""Out-of-band model-request-manifest store + async publisher.

Owns the bounded receipt window, sink queue, and usage-patch republish path so
``LLMRouter`` stays a thin facade over inference dispatch. Never mutates
provider requests — observability only.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import threading
from collections import OrderedDict, deque
from contextvars import Context


class ModelRequestManifestBus:
    """Bounded in-memory receipt window + non-blocking publish queue."""

    def __init__(self, *, maxlen: int = 32, queue_maxsize: int = 64, usage_ledger=None):
        self._items: deque = deque(maxlen=maxlen)
        self._queue_maxsize = queue_maxsize
        self._sink = None
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self.dropped_events = 0
        self.publish_failures = 0
        self.usage_ledger = usage_ledger
        self.ledger_failures = 0
        self._ledger_queue: asyncio.Queue | None = None
        self._ledger_task: asyncio.Task | None = None
        self._response_identities: OrderedDict[str, dict] = OrderedDict()
        # A lost usage record makes only its own Goal's capped accounting
        # incomplete; other Goals and unscoped chats keep exact totals.
        # Each request's reference carries its Goal, so attribution never
        # depends on these maps. They only serve callers holding a bare
        # manifest id: open requests until they settle, then the last 8192
        # settled ones, then the durable ledger row.
        self._inflight_goals: OrderedDict[str, str] = OrderedDict()
        self._manifest_goals: OrderedDict[str, str] = OrderedDict()
        self._goal_usage_losses: dict[str, int] = {}
        self._pending_partial: dict[str, dict] = {}
        self._partial_lock = threading.Lock()
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
            "usage_ledger_available": self.usage_ledger is not None,
            "usage_ledger_failures": self.ledger_failures,
            "filter_manifest_id": wanted or None,
        }

    async def record(self, manifest: dict) -> None:
        """Durably record before provider I/O, then enqueue display publication."""
        if not isinstance(manifest, dict):
            return
        stored = copy.deepcopy(manifest)
        self._remember_goal(stored)
        if self.usage_ledger is not None:
            try:
                await asyncio.to_thread(self.usage_ledger.record, stored)
            except Exception:
                self._usage_lost(self.manifest_id_from_ref(stored), self.goal_id_from_ref(stored))
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

    @staticmethod
    def goal_id_from_ref(manifest_ref) -> str:
        """The Goal a physical request ran under, from its reference or manifest."""
        if isinstance(manifest_ref, dict):
            goal_id = manifest_ref.get("goal_id")
            if not goal_id:
                run = manifest_ref.get("run")
                scope = run.get("work_scope") if isinstance(run, dict) else None
                goal_id = scope.get("goal_id") if isinstance(scope, dict) else ""
        else:
            goal_id = getattr(manifest_ref, "goal_id", "")
        return str(goal_id or "")[:160]

    def patch_usage(self, manifest_ref, normalized_usage: dict) -> None:
        """Patch and republish the exact bounded request receipt, fail-open."""
        manifest_id = self.manifest_id_from_ref(manifest_ref)
        if not manifest_id or not isinstance(normalized_usage, dict):
            return
        in_window = any(row.get('manifest_id') == manifest_id for row in self._items)
        def persist():
            self.usage_ledger.patch_usage(manifest_id, normalized_usage)
            if not in_window:
                durable = self.usage_ledger.get(manifest_id)
                if durable is not None:
                    try:
                        from observability.trace_events import record_model_usage
                        metadata = durable['metadata']
                        record_model_usage({'manifest_id': manifest_id,
                            'logical_call_id': durable['logical_call_id'],
                            'run': {'session_id': durable['session_id'], 'run_id': durable['run_id'],
                                'thread_id': metadata['thread_id'], 'source': metadata['source']},
                            'route': {'provider': durable['provider'], 'model': durable['model']},
                            'usage': copy.deepcopy(normalized_usage)})
                    except Exception:
                        self.publish_failures += 1
        self.submit_ledger(persist, manifest_id=manifest_id, usage=True,
                           goal_id=self.goal_id_from_ref(manifest_ref))
        self.settle_goal(manifest_id)
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

    def patch_stream_diagnostics(self, manifest_ref, value: dict) -> None:
        from llm_stream_diagnostics import sanitize_openai_stream_diagnostics
        cleaned = sanitize_openai_stream_diagnostics(value)
        identity = self.manifest_id_from_ref(manifest_ref)
        if cleaned and identity:
            self.submit_ledger(lambda: self.usage_ledger.patch_stream_diagnostics(identity, cleaned))

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
                "provider_generation_id",
            )
            if str(metadata.get(key) or "").strip()
        }
        if not allowed:
            return
        prior = self._response_identities.get(manifest_id, {})
        changed = {key: value for key, value in allowed.items() if prior.get(key) != value}
        if not changed:
            return
        if self.submit_ledger(lambda: self.usage_ledger.patch_response(manifest_id, changed)):
            self._response_identities[manifest_id] = {**prior, **changed}
            self._response_identities.move_to_end(manifest_id)
            if len(self._response_identities) > 8192:
                self._response_identities.popitem(last=False)
        for index in range(len(self._items) - 1, -1, -1):
            current = self._items[index]
            if str((current or {}).get("manifest_id") or "") != manifest_id:
                continue
            updated = copy.deepcopy(current)
            route = updated.get("route")
            if not isinstance(route, dict):
                route = {}
                updated["route"] = route
            route.update(changed)
            self._items[index] = updated
            self.enqueue(updated)
            return

    def submit_ledger(self, operation, *, manifest_id: str = "", usage: bool = False,
                      goal_id: str = "") -> bool:
        """Serialize blocking patches off the loop, with a bounded pending queue.

        A lost ``usage`` patch is charged to its request's Goal, which strict
        accounting then treats as incomplete. Other overflow/failures are only
        counted. Synchronous callers outside an event loop write immediately.
        """
        if self.usage_ledger is None:
            return True
        # The Goal is fixed when the write is queued, so a failure later in the
        # queue is charged to it however many requests finish meanwhile.
        goal_id = (goal_id or self._goal_of(manifest_id)) if usage else ""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                operation()
                return True
            except Exception:
                self._ledger_failed(manifest_id, usage, goal_id)
                return False
        if self._ledger_task is None or self._ledger_task.done():
            self._ledger_queue = asyncio.Queue(maxsize=256)
            self._ledger_task = loop.create_task(self._ledger_loop(), name='model-usage-writer', context=Context())
        try:
            self._ledger_queue.put_nowait((operation, manifest_id, usage, goal_id))
            return True
        except asyncio.QueueFull:
            self._ledger_failed(manifest_id, usage, goal_id)
            return False

    def submit_partial_usage(self, manifest_id: str, usage: dict, goal_id: str = "") -> bool:
        """Queue at most one partial-usage write per request; it stores the latest."""
        with self._partial_lock:
            queued = manifest_id in self._pending_partial
            self._pending_partial[manifest_id] = usage
        if queued:
            return True

        def persist():
            with self._partial_lock:
                latest = self._pending_partial.pop(manifest_id, None)
            if latest is not None:
                self.usage_ledger.patch_usage(manifest_id, latest, partial=True)

        if self.submit_ledger(persist, manifest_id=manifest_id, usage=True, goal_id=goal_id):
            return True
        with self._partial_lock:
            self._pending_partial.pop(manifest_id, None)
        return False

    def _remember_goal(self, manifest: dict) -> None:
        manifest_id = self.manifest_id_from_ref(manifest)
        scope = (manifest.get("run") or {}).get("work_scope") or {}
        goal_id = str(scope.get("goal_id") or "")[:160]
        if manifest_id and goal_id:
            self._inflight_goals[manifest_id] = goal_id
            self._inflight_goals.move_to_end(manifest_id)
            # Requests settle on final usage or when their call ends, so
            # only open calls stay here. The bound limits memory only.
            while len(self._inflight_goals) > 65536:
                self._inflight_goals.popitem(last=False)

    def settle_goal(self, manifest_id: str) -> None:
        goal_id = self._inflight_goals.pop(manifest_id, "")
        if goal_id:
            self._manifest_goals[manifest_id] = goal_id
            self._manifest_goals.move_to_end(manifest_id)
            while len(self._manifest_goals) > 8192:
                self._manifest_goals.popitem(last=False)

    def _goal_of(self, manifest_id: str) -> str:
        return self._inflight_goals.get(manifest_id) or self._manifest_goals.get(manifest_id, "")

    def _stored_goal(self, manifest_id: str) -> str:
        try:
            row = self.usage_ledger.get(manifest_id) if self.usage_ledger is not None else None
        except Exception:
            return ""
        return str((row or {}).get("goal_id") or "")[:160]

    def _usage_lost(self, manifest_id: str, goal_id: str = "") -> None:
        self.ledger_failures += 1
        goal_id = goal_id or self._goal_of(manifest_id) or self._stored_goal(manifest_id)
        if goal_id:
            self._goal_usage_losses[goal_id] = self._goal_usage_losses.get(goal_id, 0) + 1

    def _ledger_failed(self, manifest_id: str, usage: bool, goal_id: str = "") -> None:
        if usage:
            self._usage_lost(manifest_id, goal_id)
        else:
            self.ledger_failures += 1

    def goal_usage_lost(self, goal_id: str) -> int:
        """Usage records of this Goal's requests that could not be stored."""
        return self._goal_usage_losses.get(str(goal_id or ""), 0)

    async def _ledger_loop(self):
        queue = self._ledger_queue
        while True:
            item = await queue.get()
            try:
                if isinstance(item, asyncio.Future):
                    if not item.done():
                        item.set_result(None)
                else:
                    operation, manifest_id, usage, goal_id = item
                    try:
                        await asyncio.to_thread(operation)
                    except Exception:
                        self._ledger_failed(manifest_id, usage, goal_id)
            finally:
                queue.task_done()

    async def flush_ledger(self):
        """Finish pending patches before the next model/budget boundary."""
        if self._ledger_queue is not None:
            # A fence covers preceding writes, not an indefinitely busy
            # swarm's future requests. It cannot starve waiting for an empty
            # queue while other sessions continue streaming.
            fence = asyncio.get_running_loop().create_future()
            await self._ledger_queue.put(fence)
            await fence

    async def stop(self) -> None:
        """Cancel the publisher task (engine shutdown)."""
        await self.flush_ledger()
        if self._ledger_task is not None:
            self._ledger_task.cancel()
            await asyncio.gather(self._ledger_task, return_exceptions=True)
        self._ledger_task = None
        self._ledger_queue = None
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
