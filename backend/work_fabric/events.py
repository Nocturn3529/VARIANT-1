"""Append-only Work Fabric events and restart-safe outbox delivery."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .models import OutboxItem, WorkActor, WorkEvent
from .repository import WorkRepository
from .scope import WorkScope, coerce_work_scope, current_work_scope


_LOG = logging.getLogger(__name__)
EventHandler = Callable[[WorkEvent], Any]


class WorkEventService:
    """High-level event API plus an at-least-once local outbox pump."""

    def __init__(
        self,
        repository: WorkRepository,
        *,
        consumer_id: str = "",
        poll_interval_s: float = 0.5,
        lease_ttl_s: float = 30.0,
    ) -> None:
        self.repository = repository
        self.consumer_id = consumer_id or f"events:{uuid.uuid4().hex}"
        self.poll_interval_s = max(0.05, float(poll_interval_s))
        self.lease_ttl_s = max(2.0, float(lease_ttl_s))
        self._handlers: list[EventHandler] = []
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def publish(
        self,
        event_type: str,
        *,
        aggregate_kind: str,
        aggregate_id: str,
        aggregate_version: int | None = None,
        expected_aggregate_version: int | None = None,
        scope: WorkScope | Mapping[str, Any] | None = None,
        actor: WorkActor | None = None,
        correlation_id: str = "",
        causation_id: str = "",
        idempotency_key: str = "",
        payload_ref: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> WorkEvent:
        return self.repository.append_event(
            event_type=event_type,
            aggregate_kind=aggregate_kind,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            expected_aggregate_version=expected_aggregate_version,
            scope=coerce_work_scope(scope) if scope is not None else current_work_scope(),
            actor=actor,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            payload_ref=payload_ref,
            payload=payload,
        )

    def list(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 200,
        aggregate_kind: str = "",
        aggregate_id: str = "",
        event_type: str = "",
        scope: WorkScope | Mapping[str, Any] | None = None,
    ) -> list[WorkEvent]:
        return self.repository.list_events(
            after_sequence=after_sequence,
            limit=limit,
            aggregate_kind=aggregate_kind,
            aggregate_id=aggregate_id,
            event_type=event_type,
            scope=scope,
        )

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        if not callable(handler):
            raise TypeError("event handler must be callable")
        if handler not in self._handlers:
            self._handlers.append(handler)

        def unsubscribe() -> None:
            try:
                self._handlers.remove(handler)
            except ValueError:
                pass

        return unsubscribe

    async def _deliver(self, item: OutboxItem) -> None:
        for handler in tuple(self._handlers):
            result = handler(item.event)
            if inspect.isawaitable(result):
                await result

    async def dispatch_once(self, *, limit: int = 100) -> int:
        """Deliver one ordered batch; retry the whole event after any failure."""

        if not self._handlers:
            return 0
        items = await asyncio.to_thread(
            self.repository.claim_outbox,
            self.consumer_id,
            limit=limit,
            lease_ttl_s=self.lease_ttl_s,
        )
        delivered = 0
        for item in items:
            try:
                await self._deliver(item)
            except asyncio.CancelledError:
                # Leave the lease to recovery. Marking it delivered or pending
                # while a handler is being cancelled could lose an effect.
                raise
            except Exception as exc:
                delay = min(60.0, 0.5 * (2 ** min(item.attempts, 7)))
                await asyncio.to_thread(
                    self.repository.reject_outbox,
                    item.outbox_id,
                    consumer_id=self.consumer_id,
                    lease_epoch=item.lease_epoch,
                    error=f"{type(exc).__name__}: {exc}",
                    retry_delay_s=delay,
                )
                _LOG.exception("Work Fabric event delivery failed: %s", item.event.event_id)
                # Preserve global event order: do not acknowledge later items
                # after a failed earlier event in the same claim.
                break
            else:
                await asyncio.to_thread(
                    self.repository.acknowledge_outbox,
                    item.outbox_id,
                    consumer_id=self.consumer_id,
                    lease_epoch=item.lease_epoch,
                )
                delivered += 1
        return delivered

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                count = await self.dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("Work Fabric outbox pump failed")
                count = 0
            if count:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.poll_interval_s
                )
            except asyncio.TimeoutError:
                pass

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name="variant1-work-event-outbox"
        )

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        self._stopping.set()
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


__all__ = ["EventHandler", "WorkEventService"]
