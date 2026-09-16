"""Automation-aware LLM scheduler and per-run principal identity.

Two ideas live here:

- **Per-agent identity (``Principal``).** Every run acts AS someone: the
  interactive user, a subagent, or a scheduled automation.
  ``principal_for`` derives that identity (name + kind + priority) from the run
  context's source, so the rest of the system can reason about WHO is asking —
  not just that "an agent" is. This is the concurrency-safe replacement for the
  old single ``_ACTIVE_SESSION`` global (which could only represent one actor).

- **Priority scheduling (``LLMScheduler``).** An AI host on consumer hardware
  has ONE local engine, and background work such as an inference benchmark
  must never make the person waiting on a live
  reply queue behind it. The scheduler is a priority gate around local
  generation: when the engine is busy, waiters are admitted highest-priority
  first, so an interactive request jumps ahead of any queued background work.

Priority is per-CALL, not per-task: each LLM step acquires and releases, so an
interactive request waits at most for one in-flight background STEP (a few
seconds), never for a whole background task. Cloud generation doesn't contend
for the local engine, so it bypasses the gate entirely (wired in the router).
"""

from __future__ import annotations

import asyncio
import heapq
from contextlib import asynccontextmanager
from dataclasses import dataclass

# Priority tiers (lower = served first).
INTERACTIVE = 0        # the person is waiting on this reply
SERVICE = 1            # user-requested work that isn't the live turn
BACKGROUND = 2         # unprompted background upkeep

# Run source -> priority. Unbound/interactive chat maps to INTERACTIVE; anything
# unknown defaults to SERVICE (safer to slightly under- than over-prioritize).
KIND_PRIORITY = {
    "chat": INTERACTIVE, "user": INTERACTIVE, "interactive": INTERACTIVE,
    "subagent": SERVICE, "automation": SERVICE,
    "benchmark": BACKGROUND,
}

_KIND_LABEL = {
    "chat": "You", "user": "You", "interactive": "You",
    "subagent": "Subagent", "automation": "Automation",
    "benchmark": "Inference benchmark",
}

_PRIORITY_LABEL = {INTERACTIVE: "interactive", SERVICE: "service", BACKGROUND: "background"}


@dataclass(frozen=True)
class Principal:
    """WHO a run acts as. ``name`` is human-facing; ``kind`` is the run source;
    ``priority`` drives scheduling (lower = sooner)."""

    name: str
    kind: str
    priority: int

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "priority": self.priority,
                "tier": _PRIORITY_LABEL.get(self.priority, "service")}


def principal_for(source: str | None, title: str = "") -> Principal:
    kind = (source or "chat").strip().lower() or "chat"
    priority = KIND_PRIORITY.get(kind, SERVICE)
    name = (title or "").strip() or _KIND_LABEL.get(kind, kind.replace("_", " ").title())
    return Principal(name=name[:80], kind=kind, priority=priority)


def principal_for_context(ctx) -> Principal:
    """Identity of the currently-bound run (or the interactive user when no run
    context is bound — the live chat turn's LLM calls)."""
    if ctx is None:
        return principal_for("chat")
    return principal_for(getattr(ctx, "source", "chat"), getattr(ctx, "title", ""))


class LLMScheduler:
    """Priority gate serializing access to the single local engine (concurrency
    1). Waiters are admitted highest-priority first, FIFO within a tier."""

    def __init__(self):
        self._busy = False
        self._active: Principal | None = None
        self._waiters: list = []          # heap of (priority, seq, future, principal)
        self._seq = 0
        self._served = 0

    async def acquire(self, principal: Principal) -> None:
        if not self._busy:
            self._busy = True
            self._active = principal
            self._served += 1
            return
        fut = asyncio.get_event_loop().create_future()
        self._seq += 1
        heapq.heappush(self._waiters, (principal.priority, self._seq, fut, principal))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # release() already handed us the slot, but we're cancelled
                # before we can hold it. Dropping it now would leave the engine
                # busy with no holder (permanent deadlock), so pass the slot on
                # to the next waiter instead of leaking it.
                self.release()
            else:
                # Still queued — drop our slot from the heap so a release()
                # never hands the engine to a gone waiter.
                self._waiters = [w for w in self._waiters if w[2] is not fut]
                heapq.heapify(self._waiters)
            raise
        self._active = principal
        self._served += 1

    def release(self) -> None:
        while self._waiters:
            _pri, _seq, fut, _principal = heapq.heappop(self._waiters)
            if fut.cancelled():
                continue
            fut.set_result(None)          # hand off; stays busy for the next holder
            return
        self._busy = False
        self._active = None

    @asynccontextmanager
    async def slot(self, principal: Principal):
        await self.acquire(principal)
        try:
            yield
        finally:
            self.release()

    def status(self) -> dict:
        return {
            "busy": self._busy,
            "active": self._active.to_dict() if self._active else None,
            "queue_depth": len(self._waiters),
            "queued": [w[3].to_dict() for w in sorted(self._waiters)],
            "served": self._served,
        }
