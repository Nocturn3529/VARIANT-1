"""Ownership and exception reporting for intentional fire-and-forget tasks.

``asyncio`` keeps only weak references to scheduled tasks. Every coroutine that
outlives its caller should therefore either be owned by a domain object/lifespan
or be registered here until completion.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional


def _default_reporter(label: str, exc: BaseException) -> None:
    print(f"[background] task={label} failed: {exc}", flush=True)


class OwnedTaskSet:
    """Strong ownership plus cancellation-safe settlement for async tasks."""

    def __init__(self) -> None:
        self._active: set[asyncio.Task] = set()

    def spawn(
        self,
        awaitable: Awaitable,
        *,
        name: str = "background",
        reporter: Optional[Callable[[str, BaseException], None]] = None,
    ) -> asyncio.Task:
        task = asyncio.create_task(awaitable, name=name)
        self._active.add(task)

        def completed(done: asyncio.Task) -> None:
            self._active.discard(done)
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                (reporter or _default_reporter)(name, exc)

        task.add_done_callback(completed)
        return task

    def active_count(self) -> int:
        return len(self._active)

    async def cancel_all(self) -> None:
        tasks = list(self._active)
        for task in tasks:
            task.cancel()
        if not tasks:
            return

        async def settle() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)

        settling = asyncio.create_task(settle(), name="owned-task-settle")
        cancellation: asyncio.CancelledError | None = None
        while not settling.done():
            try:
                await asyncio.shield(settling)
            except asyncio.CancelledError as exc:
                # Cleanup owns its children to completion even if shutdown is
                # cancelled repeatedly by an outer deadline.
                cancellation = exc
                continue
        settling.result()
        if cancellation is not None:
            raise cancellation


_OWNER = OwnedTaskSet()


def spawn(
    awaitable: Awaitable,
    *,
    name: str = "background",
    reporter: Optional[Callable[[str, BaseException], None]] = None,
) -> asyncio.Task:
    return _OWNER.spawn(awaitable, name=name, reporter=reporter)


def active_count() -> int:
    """Return the number of registered tasks, primarily for health/tests."""
    return _OWNER.active_count()


async def cancel_all() -> None:
    """Cancel and settle all registered work during backend shutdown."""
    await _OWNER.cancel_all()


__all__ = ["OwnedTaskSet", "active_count", "cancel_all", "spawn"]
