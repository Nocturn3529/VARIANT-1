"""LLM scheduler + per-agent identity (track 3): principal derivation and the
priority gate.

Invariants pinned here:
  - Interactive requests are admitted before queued background work, regardless
    of arrival order; ties within a tier are FIFO.
  - A cancelled waiter is removed from the queue and never handed the engine.
  - Cloud/no-op paths (no gate) are unaffected — that lives in the router test
    surface; here we prove the gate's ordering directly.
"""

from __future__ import annotations

import asyncio

import pytest

from automation.scheduler import (BACKGROUND, INTERACTIVE, SERVICE, LLMScheduler,
                             Principal, principal_for, principal_for_context)


# ---- identity -------------------------------------------------------------------

def test_principal_priorities_by_source():
    assert principal_for("chat").priority == INTERACTIVE
    assert principal_for(None).priority == INTERACTIVE            # unbound = interactive
    assert principal_for("automation").priority == SERVICE
    assert principal_for("benchmark").priority == BACKGROUND
    assert principal_for("weird-source").priority == SERVICE      # default


def test_principal_name_and_context():
    assert principal_for("chat").name == "You"
    assert principal_for("automation", "Morning brief").name == "Morning brief"

    class Ctx:
        source = "benchmark"; title = ""
    p = principal_for_context(Ctx())
    assert p.kind == "benchmark" and p.priority == BACKGROUND
    assert principal_for_context(None).priority == INTERACTIVE
    assert p.to_dict()["tier"] == "background"


# ---- priority gate --------------------------------------------------------------

@pytest.mark.asyncio
async def test_immediate_acquire_when_free():
    s = LLMScheduler()
    async with s.slot(principal_for("chat")):
        assert s.status()["busy"] is True
        assert s.status()["active"]["kind"] == "chat"
    assert s.status()["busy"] is False


@pytest.mark.asyncio
async def test_interactive_jumps_ahead_of_queued_background():
    s = LLMScheduler()
    order = []

    # Holder occupies the engine; two waiters queue while it's busy.
    await s.acquire(principal_for("automation", "holder"))

    async def run(principal, tag):
        async with s.slot(principal):
            order.append(tag)
            await asyncio.sleep(0.01)

    bg = asyncio.create_task(run(principal_for("benchmark"), "background"))
    await asyncio.sleep(0.005)                    # ensure bg is queued first
    inter = asyncio.create_task(run(principal_for("chat"), "interactive"))
    await asyncio.sleep(0.005)                    # ensure both queued

    assert s.status()["queue_depth"] == 2
    # Queue preview is priority-ordered: interactive first despite arriving later.
    assert s.status()["queued"][0]["kind"] == "chat"

    s.release()                                   # holder done → highest priority next
    await asyncio.gather(bg, inter)
    assert order == ["interactive", "background"]


@pytest.mark.asyncio
async def test_fifo_within_same_tier():
    s = LLMScheduler()
    order = []
    await s.acquire(principal_for("chat", "holder"))

    async def run(tag):
        async with s.slot(principal_for("automation", tag)):
            order.append(tag)

    a = asyncio.create_task(run("a")); await asyncio.sleep(0.005)
    b = asyncio.create_task(run("b")); await asyncio.sleep(0.005)
    c = asyncio.create_task(run("c")); await asyncio.sleep(0.005)
    s.release()
    await asyncio.gather(a, b, c)
    assert order == ["a", "b", "c"]               # same priority → arrival order


@pytest.mark.asyncio
async def test_cancelled_waiter_is_dropped():
    s = LLMScheduler()
    await s.acquire(principal_for("chat", "holder"))

    async def waiter():
        async with s.slot(principal_for("automation", "doomed")):
            pass
    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.005)
    assert s.status()["queue_depth"] == 1
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert s.status()["queue_depth"] == 0         # removed from the heap

    # The engine still releases cleanly and serves the next real request.
    s.release()
    async with s.slot(principal_for("chat")):
        assert s.status()["active"]["kind"] == "chat"


@pytest.mark.asyncio
async def test_cancel_during_handoff_does_not_leak_slot():
    """A waiter cancelled in the window AFTER release() handed it the slot but
    BEFORE it runs must pass the slot on, not leak it. Regression: acquire()'s
    cancel handler only cleaned up still-queued waiters, so a handed-off-then-
    cancelled waiter left the engine busy with no holder → permanent deadlock."""
    s = LLMScheduler()
    await s.acquire(principal_for("chat", "holder"))

    async def waiter():
        async with s.slot(principal_for("automation", "doomed")):
            pass

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.005)
    assert s.status()["queue_depth"] == 1

    # Hand the slot to the waiter, then cancel it before the loop resumes it.
    # No await between these two lines, so the waiter is still parked on its
    # (now-resolved) future when the cancellation lands.
    s.release()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t

    # Slot was passed on (no other waiters) → engine free, not wedged.
    assert s.status()["busy"] is False
    assert s.status()["queue_depth"] == 0
    # And it still serves the next request instead of queueing forever.
    async with s.slot(principal_for("chat")):
        assert s.status()["active"]["kind"] == "chat"


@pytest.mark.asyncio
async def test_served_counter_and_release_when_empty():
    s = LLMScheduler()
    for _ in range(3):
        async with s.slot(principal_for("chat")):
            pass
    st = s.status()
    assert st["served"] == 3 and st["busy"] is False and st["active"] is None
