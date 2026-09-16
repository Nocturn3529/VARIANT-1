from __future__ import annotations

import pytest

from goals.resource_cleanup import (
    GoalChildRoot,
    cleanup_goal_descendant_resources,
)


class FakeChildren:
    def __init__(self, rows, *, truncated_parents=(), stuck=()):
        self.rows = {row["child_id"]: dict(row) for row in rows}
        self.truncated_parents = set(truncated_parents)
        self.stuck = set(stuck)
        self.cancelled = []

    def inspect(self, parent_chat_id, child_id):
        row = self.rows[child_id]
        if row["parent_chat_id"] != parent_chat_id:
            raise LookupError("unknown child handle")
        return dict(row)

    def tree(self, parent_chat_id, *, limit=100):
        found = []
        frontier = [(parent_chat_id, 1)]
        while frontier:
            parent, level = frontier.pop(0)
            for row in self.rows.values():
                if row["parent_chat_id"] != parent:
                    continue
                found.append({**row, "depth": level})
                frontier.append((row["child_chat_id"], level + 1))
        return {
            "items": found[:limit],
            "truncated": (
                parent_chat_id in self.truncated_parents or len(found) > limit
            ),
        }

    async def cancel(self, parent_chat_id, child_id):
        row = self.rows[child_id]
        assert row["parent_chat_id"] == parent_chat_id
        self.cancelled.append(child_id)
        if child_id not in self.stuck:
            row["status"] = "cancelled"
        return dict(row)


class FakeUnboundedChildren(FakeChildren):
    def __init__(self, rows):
        super().__init__(rows)
        self.used_unbounded = False

    def descendants_for_cleanup(self, parent_chat_id):
        self.used_unbounded = True
        result = super().tree(parent_chat_id, limit=10_000)
        return result["items"]

    def tree(self, parent_chat_id, *, limit=100):
        raise AssertionError("bounded model-facing tree must not be used")


class FakeExecutionRepository:
    def __init__(self, live=None):
        self.live = {
            chat_id: (set(terminals), set(processes))
            for chat_id, (terminals, processes) in (live or {}).items()
        }

    def live_ids_for_chat(self, chat_id):
        terminals, processes = self.live.get(chat_id, (set(), set()))
        return tuple(sorted(terminals)), tuple(sorted(processes))


class FakeExecution:
    def __init__(self, live=None, *, failures=()):
        self.repository = FakeExecutionRepository(live)
        self.failures = set(failures)
        self.deleted = []

    async def delete_chat(self, chat_id):
        self.deleted.append(chat_id)
        if chat_id in self.failures:
            raise RuntimeError("execution cleanup failed")
        terminals, processes = self.repository.live.get(
            chat_id, (set(), set())
        )
        count = len(terminals) + len(processes)
        self.repository.live[chat_id] = (set(), set())
        return count


class FakeKernel:
    def __init__(self, live=(), *, failures=()):
        self.live = set(live)
        self.failures = set(failures)
        self.closed = []

    async def close_chat(self, chat_id, *, reason):
        self.closed.append((chat_id, reason))
        if chat_id in self.failures:
            raise RuntimeError("kernel cleanup failed")
        existed = chat_id in self.live
        self.live.discard(chat_id)
        return existed


def child(child_id, parent, chat, status="completed"):
    return {
        "child_id": child_id,
        "parent_chat_id": parent,
        "child_chat_id": chat,
        "status": status,
    }


@pytest.mark.asyncio
async def test_terminal_child_still_cleans_owned_process_and_preserves_unrelated():
    children = FakeChildren([
        child("owned", "parent", "owned-chat", "completed"),
        child("other", "parent", "other-chat", "running"),
    ])
    execution = FakeExecution({
        "owned-chat": (("term-owned",), ("proc-owned",)),
        "other-chat": (("term-other",), ("proc-other",)),
    })
    kernel = FakeKernel({"owned-chat", "other-chat"})

    result = await cleanup_goal_descendant_resources(
        goal_id="goal-1",
        roots=[GoalChildRoot("owned", "parent", "owned-chat", "effect-1")],
        child_manager=children,
        execution=execution,
        kernel=kernel,
    )

    assert result.complete
    assert result.stopped_process_ids == ("proc-owned",)
    assert result.closed_terminal_ids == ("term-owned",)
    assert result.closed_kernel_chat_ids == ("owned-chat",)
    assert children.cancelled == []
    assert execution.repository.live_ids_for_chat("other-chat") == (
        ("term-other",), ("proc-other",),
    )
    assert "other-chat" in kernel.live


@pytest.mark.asyncio
async def test_descendants_cancel_and_cleanup_deepest_first():
    children = FakeChildren([
        child("root", "parent", "root-chat", "running"),
        child("nested", "root-chat", "nested-chat", "running"),
    ])
    execution = FakeExecution({
        "root-chat": ((), ("root-proc",)),
        "nested-chat": (("nested-term",), ("nested-proc",)),
    })
    kernel = FakeKernel({"root-chat", "nested-chat"})

    result = await cleanup_goal_descendant_resources(
        goal_id="goal-2",
        roots=[{"child_id": "root", "parent_chat_id": "parent",
                "child_chat_id": "root-chat", "effect_id": "effect-2"}],
        child_manager=children,
        execution=execution,
        kernel=kernel,
    )

    assert result.complete
    assert children.cancelled == ["nested", "root"]
    assert execution.deleted == [
        "nested-chat", "nested-chat", "root-chat", "root-chat",
    ]
    assert result.descendant_chat_ids == ("nested-chat", "root-chat")
    assert set(result.stopped_process_ids) == {"nested-proc", "root-proc"}
    assert result.closed_terminal_ids == ("nested-term",)


@pytest.mark.asyncio
async def test_incomplete_execution_cleanup_is_retryable_and_never_success():
    children = FakeChildren([
        child("owned", "parent", "owned-chat", "cancelled"),
    ])
    execution = FakeExecution(
        {"owned-chat": ((), ("proc-owned",))},
        failures={"owned-chat"},
    )
    kernel = FakeKernel({"owned-chat"})
    root = GoalChildRoot("owned", "parent", "owned-chat")

    first = await cleanup_goal_descendant_resources(
        goal_id="goal-3", roots=[root], child_manager=children,
        execution=execution, kernel=kernel,
    )
    assert not first.complete
    assert any(item["id"] == "proc-owned" for item in first.remaining)
    assert {issue.phase for issue in first.issues} >= {
        "execution", "execution_after_kernel", "verify_after_kernel",
    }

    execution.failures.clear()
    second = await cleanup_goal_descendant_resources(
        goal_id="goal-3", roots=[root], child_manager=children,
        execution=execution, kernel=kernel,
    )
    assert second.complete
    assert second.stopped_process_ids == ("proc-owned",)


@pytest.mark.asyncio
async def test_truncated_descendant_discovery_cannot_report_complete():
    children = FakeChildren(
        [child("root", "parent", "root-chat", "completed")],
        truncated_parents={"root-chat"},
    )
    result = await cleanup_goal_descendant_resources(
        goal_id="goal-4",
        roots=[GoalChildRoot("root", "parent", "root-chat")],
        child_manager=children,
        execution=FakeExecution(),
        kernel=FakeKernel(),
    )

    assert not result.discovery_complete
    assert not result.complete
    assert any(issue.resource_kind == "descendant_tree" for issue in result.issues)


@pytest.mark.asyncio
async def test_catalog_identity_mismatch_does_not_authorize_cleanup():
    children = FakeChildren([
        child("owned", "parent", "different-chat", "completed"),
    ])
    execution = FakeExecution({
        "claimed-chat": ((), ("unrelated-proc",)),
    })
    kernel = FakeKernel({"claimed-chat"})

    result = await cleanup_goal_descendant_resources(
        goal_id="goal-5",
        roots=[GoalChildRoot("owned", "parent", "claimed-chat")],
        child_manager=children,
        execution=execution,
        kernel=kernel,
    )

    assert not result.complete
    assert execution.deleted == []
    assert execution.repository.live_ids_for_chat("claimed-chat")[1] == (
        "unrelated-proc",
    )
    assert kernel.closed == []


@pytest.mark.asyncio
async def test_nonterminal_child_after_cancel_remains_explicitly_pending():
    children = FakeChildren(
        [child("owned", "parent", "owned-chat", "running")],
        stuck={"owned"},
    )
    result = await cleanup_goal_descendant_resources(
        goal_id="goal-6",
        roots=[GoalChildRoot("owned", "parent", "owned-chat")],
        child_manager=children,
        execution=FakeExecution(),
        kernel=FakeKernel(),
    )

    assert not result.complete
    assert any(item["kind"] == "child" for item in result.remaining)
    assert any(issue.phase == "cancel_child" for issue in result.issues)


@pytest.mark.asyncio
async def test_prefers_unbounded_internal_descendant_enumeration():
    rows = [child("root", "parent", "root-chat", "completed")]
    parent = "root-chat"
    for index in range(105):
        chat_id = f"nested-chat-{index}"
        rows.append(child(f"nested-{index}", parent, chat_id, "completed"))
        parent = chat_id
    children = FakeUnboundedChildren(rows)

    result = await cleanup_goal_descendant_resources(
        goal_id="goal-unbounded",
        roots=[GoalChildRoot("root", "parent", "root-chat")],
        child_manager=children,
        execution=FakeExecution(),
        kernel=FakeKernel(),
    )

    assert children.used_unbounded
    assert result.discovery_complete
    assert result.complete
    assert len(result.descendant_chat_ids) == 106


@pytest.mark.asyncio
async def test_post_kernel_sweep_stops_process_published_after_first_snapshot():
    children = FakeChildren([
        child("owned", "parent", "owned-chat", "completed"),
    ])
    execution = FakeExecution()

    class PublishingKernel(FakeKernel):
        async def close_chat(self, chat_id, *, reason):
            execution.repository.live[chat_id] = (set(), {"late-process"})
            return await super().close_chat(chat_id, reason=reason)

    result = await cleanup_goal_descendant_resources(
        goal_id="goal-late-process",
        roots=[GoalChildRoot("owned", "parent", "owned-chat")],
        child_manager=children,
        execution=execution,
        kernel=PublishingKernel({"owned-chat"}),
    )

    assert result.complete
    assert result.stopped_process_ids == ("late-process",)
    assert execution.repository.live_ids_for_chat("owned-chat") == ((), ())


@pytest.mark.asyncio
async def test_descendant_appearing_during_cleanup_is_exposed_for_retry():
    children = FakeUnboundedChildren([
        child("owned", "parent", "owned-chat", "completed"),
    ])

    class SpawningKernel(FakeKernel):
        async def close_chat(self, chat_id, *, reason):
            if "late" not in children.rows:
                children.rows["late"] = child(
                    "late", "owned-chat", "late-chat", "running",
                )
            return await super().close_chat(chat_id, reason=reason)

    result = await cleanup_goal_descendant_resources(
        goal_id="goal-late-child",
        roots=[GoalChildRoot("owned", "parent", "owned-chat")],
        child_manager=children,
        execution=FakeExecution(),
        kernel=SpawningKernel({"owned-chat"}),
    )

    assert not result.complete
    assert not result.discovery_complete
    assert any(
        item["id"] == "late"
        and item["state"] == "discovered_after_producer_close"
        for item in result.remaining
    )
    assert any(issue.phase == "rescan" for issue in result.issues)
