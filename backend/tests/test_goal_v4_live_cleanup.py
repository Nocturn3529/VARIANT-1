from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from artifacts import ContentAddressedArtifactStore
from execution_hosts import ExecutionOwner, create_execution_runtime
from goals import create_goal_service
from goals.host_handlers import build_goal_host_handlers
from goals.models import GoalConflict
from work_fabric.scope import WorkScope
from work_fabric.service import WorkService


class ChildRows:
    def __init__(self, *, status: str):
        self.row = {
            "child_id": "goal-child",
            "parent_chat_id": "parent-chat",
            "child_chat_id": "goal-child-chat",
            "status": status,
            "depth": 1,
        }
        self.cancelled = 0

    def inspect(self, parent_chat_id, child_id):
        if (
            parent_chat_id != self.row["parent_chat_id"]
            or child_id != self.row["child_id"]
        ):
            raise LookupError("unknown child handle")
        return dict(self.row)

    def descendants_for_cleanup(self, chat_id):
        assert chat_id == self.row["child_chat_id"]
        return []

    async def cancel(self, parent_chat_id, child_id):
        self.inspect(parent_chat_id, child_id)
        self.cancelled += 1
        self.row["status"] = "cancelled"
        return dict(self.row)


class KernelRows:
    def __init__(self):
        self.closed = []

    async def close_chat(self, chat_id, *, reason):
        self.closed.append((chat_id, reason))
        return False

    async def interrupt(self, chat_id, *, intent="stop"):
        return {
            "runtime_chat_id": chat_id,
            "operation": "interrupt",
            "status": "absent",
            "intent": intent,
        }


async def _wait_for_counter(path: Path, *, minimum: int = 3) -> int:
    for _ in range(200):
        try:
            value = int(path.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, PermissionError, ValueError):
            value = 0
        if value >= minimum:
            return value
        await asyncio.sleep(0.025)
    raise AssertionError("heartbeat process did not publish a counter")


def _heartbeat_command(path: Path) -> list[str]:
    program = (
        "from pathlib import Path\n"
        "import os,sys,time\n"
        "target=Path(sys.argv[1]); count=0\n"
        "while True:\n"
        " count += 1\n"
        " temp=target.with_suffix('.tmp')\n"
        " while True:\n"
        "  try:\n"
        "   temp.write_text(str(count),encoding='utf-8')\n"
        "   os.replace(temp,target)\n"
        "   break\n"
        "  except PermissionError:\n"
        "   time.sleep(0.005)\n"
        " time.sleep(0.05)\n"
    )
    return [sys.executable, "-u", "-c", program, str(path)]


@pytest.mark.asyncio
@pytest.mark.parametrize("child_status", ["completed", "running"])
async def test_cancel_async_stops_terminal_or_active_child_owned_heartbeat(
    tmp_path, child_status,
):
    artifacts = ContentAddressedArtifactStore(str(tmp_path / "artifacts"))
    work = WorkService.open(
        str(tmp_path / "work.sqlite3"), worker_id="goal-v4-cleanup-test",
    )
    execution = create_execution_runtime(
        data_dir=str(tmp_path / "execution"),
        artifact_store=artifacts,
        backend_instance_id="goal-v4-cleanup-test",
    )
    children = ChildRows(status=child_status)
    kernel = KernelRows()
    runtime = SimpleNamespace(
        execution=execution,
        kernel=kernel,
        catalog=SimpleNamespace(children=children),
        session_artifacts=artifacts,
        coding=None,
    )
    host = SimpleNamespace(
        app_root=str(tmp_path),
        session_artifacts=artifacts,
        require_runtime=lambda: runtime,
    )
    goals = create_goal_service(work, register_work_handler=False)
    handlers = build_goal_host_handlers(host, goals)
    goals.register_cancellation_handler(handlers.cancel_goal_resources)
    for kind, handler in handlers.handlers().items():
        goals.executor.register(kind, handler)
    for source, resolver in handlers.wait_resolvers().items():
        goals.register_wait_resolver(source, resolver)

    owned_heartbeat = tmp_path / "owned-heartbeat.txt"
    owned = await execution.start_process(
        _heartbeat_command(owned_heartbeat),
        owner=ExecutionOwner(
            "chat", "goal-child-chat",
            WorkScope(chat_id="goal-child-chat"),
        ),
        cwd=str(tmp_path),
        process_id="owned-heartbeat",
    )
    unrelated = await execution.start_process(
        [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
        owner=ExecutionOwner(
            "chat", "unrelated-chat", WorkScope(chat_id="unrelated-chat"),
        ),
        cwd=str(tmp_path),
        process_id="unrelated-process",
    )

    try:
        await _wait_for_counter(owned_heartbeat)
        goal = goals.create(
            title="Stop owned child resources",
            objective="Retain a heartbeat until explicit Stop",
            owner_chat_id="parent-chat",
            completion_policy={"entrypoint": "composer_goal"},
        )
        goal = goals.plan(
            goal.goal_id,
            [{"step_id": "child", "kind": "agent", "instructions": "run"}],
            expected_version=goal.version,
        )
        goal = goals.start(
            goal.goal_id, expected_version=goal.version, enqueue=False,
        )
        effect = goals.repository.record_effect(
            goal.goal_id,
            "child",
            expected_version=goal.version,
            kind="agent.spawn",
            idempotency_key="goal-v4-child",
            request={"parent_chat_id": "parent-chat"},
            status="planned",
        )
        current = goals.get(goal.goal_id)
        goals.repository.update_effect(
            effect.effect_id,
            expected_version=current.version,
            status="dispatched",
            response={
                "child_id": "goal-child",
                "child_chat_id": "goal-child-chat",
                "status": child_status,
            },
        )

        current = goals.get(goal.goal_id)
        cancelled = await goals.cancel_async(
            goal.goal_id,
            expected_version=current.version,
            reason="operator Stop",
            correlation_id=f"stop-{child_status}",
        )
        assert cancelled.status == "cancelled"

        stopped_at = int(owned_heartbeat.read_text(encoding="utf-8").strip())
        await asyncio.sleep(0.25)
        stable = int(owned_heartbeat.read_text(encoding="utf-8").strip())
        assert stable == stopped_at
        assert not execution.processes.get(owned.process_id).live
        assert execution.processes.get(unrelated.process_id).live

        receipt = goals.repository.state_get(
            goal.goal_id, "resource_cleanup",
        )
        assert receipt["status"] == "complete"
        assert receipt["complete"] is True
        assert owned.process_id in receipt["stopped_process_ids"]
        assert receipt["remaining"] == []
        assert receipt["issues"] == []
        assert children.cancelled == int(child_status == "running")
        assert kernel.closed == [("goal-child-chat", "goal_cancelled")]
    finally:
        for process_id in (owned.process_id, unrelated.process_id):
            try:
                execution.processes.stop(process_id, force=True)
            except Exception:
                pass
        execution.shutdown()
        await work.shutdown()


def _cleanup_goal(goals):
    goal = goals.create(
        title="Cleanup token",
        objective="Exercise cleanup serialization",
        owner_chat_id="parent-chat",
    )
    goal = goals.plan(
        goal.goal_id,
        [{"step_id": "wait", "kind": "wait",
          "wait_spec": {"source": "time", "delay_s": 60}}],
        expected_version=goal.version,
    )
    return goals.start(goal.goal_id, expected_version=goal.version, enqueue=False)


@pytest.mark.asyncio
async def test_newer_cleanup_token_cannot_be_overwritten_by_older_result(tmp_path):
    work = WorkService.open(
        str(tmp_path / "work.sqlite3"), worker_id="cleanup-token-test",
    )
    goals = create_goal_service(work, register_work_handler=False)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def cleanup(_goal, _reason):
        nonlocal calls
        calls += 1
        call = calls
        if call == 1:
            first_entered.set()
            await release_first.wait()
        return {"complete": True, "cleanup_call": call}

    goals.register_cancellation_handler(cleanup)
    goal = _cleanup_goal(goals)
    try:
        first = asyncio.create_task(goals.cancel_async(
            goal.goal_id,
            expected_version=goal.version,
            reason="first cleanup",
            correlation_id="cleanup-one",
        ))
        await asyncio.wait_for(first_entered.wait(), 2)
        after_first = goals.get(goal.goal_id)
        first_token = goals.repository.state_get(
            goal.goal_id, "resource_cleanup",
        )["token"]

        second = asyncio.create_task(goals.cancel_async(
            goal.goal_id,
            expected_version=after_first.version,
            reason="second cleanup",
            correlation_id="cleanup-two",
        ))
        for _ in range(100):
            pending = goals.repository.state_get(
                goal.goal_id, "resource_cleanup",
            )
            if pending.get("token") != first_token:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("newer cleanup token was not committed")
        second_token = pending["token"]
        assert second_token.startswith("cleanup-two:")

        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), 3)

        final = goals.repository.state_get(goal.goal_id, "resource_cleanup")
        assert calls == 2
        assert final["token"] == second_token
        assert final["status"] == "complete"
        assert final["complete"] is True
        assert final["cleanup_call"] == 2
    finally:
        release_first.set()
        await work.shutdown()


@pytest.mark.asyncio
async def test_cleanup_receipt_retries_version_conflict_for_same_token(
    tmp_path, monkeypatch,
):
    work = WorkService.open(
        str(tmp_path / "work.sqlite3"), worker_id="cleanup-cas-test",
    )
    goals = create_goal_service(work, register_work_handler=False)
    goals.register_cancellation_handler(
        lambda _goal, _reason: {"complete": True, "proof": "settled"},
    )
    goal = _cleanup_goal(goals)
    original = goals.repository.state_set
    conflicts = 0

    def racing_state_set(goal_id, key, value, **kwargs):
        nonlocal conflicts
        if (
            key == "resource_cleanup"
            and isinstance(value, dict)
            and value.get("status") == "complete"
            and conflicts < 2
        ):
            conflicts += 1
            latest = goals.repository.require_goal(goal_id)
            original(
                goal_id,
                f"unrelated-{conflicts}",
                {"revision": conflicts},
                expected_version=latest.version,
            )
            raise GoalConflict("injected concurrent goal state change")
        return original(goal_id, key, value, **kwargs)

    monkeypatch.setattr(goals.repository, "state_set", racing_state_set)
    try:
        result = await goals.cancel_async(
            goal.goal_id,
            expected_version=goal.version,
            reason="CAS cleanup",
            correlation_id="cleanup-cas",
        )
        receipt = goals.repository.state_get(
            goal.goal_id, "resource_cleanup",
        )
        assert result.status == "cancelled"
        assert conflicts == 2
        assert receipt["status"] == "complete"
        assert receipt["complete"] is True
        assert receipt["proof"] == "settled"
        assert receipt["token"].startswith("cleanup-cas:")
    finally:
        await work.shutdown()
