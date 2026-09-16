from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from goals import create_goal_service
from goals.executor import StepExecutionContext
from goals.host_handlers import GoalHostHandlers
from goals.models import GoalConflict
from work_fabric.scope import WorkScope
from work_fabric.service import WorkService


@pytest.mark.asyncio
async def test_deleted_chat_cancels_every_work_job_beyond_public_list_cap(tmp_path):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    scope = WorkScope(chat_id="chat-to-delete")
    for _ in range(1001):
        work.jobs.create("test.pending", owner_kind="chat", owner_id=scope.chat_id, scope=scope)
    try:
        assert await work.delete_chat(scope.chat_id) == 1001
        remaining = work.jobs.list(
            chat_id=scope.chat_id, statuses=("queued", "leased", "running", "waiting", "paused"),
            cancel_requested=False, limit=1000,
        )
        assert remaining == []
        assert await work.delete_chat("") == 0
    finally:
        await work.shutdown()


@pytest.mark.asyncio
async def test_expired_synchronous_handler_cannot_be_automatically_released_for_retry(tmp_path):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def handler(context):
        calls.append(context.lease_epoch)
        entered.set()
        release.wait(5)

    work.register_job_handler("test.sync", handler)
    job = work.jobs.create(
        "test.sync", owner_kind="system", owner_id="test", max_attempts=3,
        retry_policy={"on_lease_expiry": "retry", "base_delay_s": 0},
    )
    try:
        await work.scheduler.run_once()
        assert await asyncio.to_thread(entered.wait, 3)
        with work.repository._write() as conn:
            row = conn.execute(
                "SELECT sync_handler FROM work_job WHERE job_id=?", (job.job_id,),
            ).fetchone()
            assert row[0] == 1
            conn.execute("UPDATE work_job SET lease_expires_at=0 WHERE job_id=?", (job.job_id,))
        work.recovery.run_once()
        assert work.jobs.require(job.job_id).status == "unknown_effect"
        assert await work.scheduler.run_once() == 0
        assert calls == [1]
    finally:
        release.set()
        await work.scheduler.shutdown()
        await work.shutdown()


def test_old_running_work_row_without_mode_column_migrates_conservatively(tmp_path):
    path = str(tmp_path / "work.sqlite3")
    work = WorkService.open(path)
    job = work.jobs.create(
        "test.old-running", owner_kind="system", owner_id="test", max_attempts=3,
        retry_policy={"on_lease_expiry": "retry"},
    )
    leased = work.repository.lease_next_job("old-worker")
    assert leased.job_id == job.job_id
    work.repository.start_job(
        job.job_id, lease_owner=leased.lease_owner, lease_epoch=leased.lease_epoch,
    )
    with work.repository._write() as conn:
        conn.execute("ALTER TABLE work_job DROP COLUMN sync_handler")
    migrated = WorkService.open(path)
    with migrated.repository._read() as conn:
        row = conn.execute(
            "SELECT sync_handler FROM work_job WHERE job_id=?", (job.job_id,),
        ).fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_supervisor_preserves_cancelled_step_instead_of_mapping_it_to_failed(tmp_path):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))

    async def cancelled(_context):
        raise asyncio.CancelledError()

    goals = create_goal_service(work, handlers={"python": cancelled})
    goal = goals.create(
        title="Cancellation", objective="Stop accurately", owner_chat_id="chat-goal",
    )
    goal = goals.plan(
        goal.goal_id, [{"step_id": "cancelled", "kind": "python"}],
        expected_version=goal.version,
    )
    goals.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    try:
        await goals.supervisor.tick(goal.goal_id)
        assert goals.repository.get_step(goal.goal_id, "cancelled").status == "cancelled"
    finally:
        await work.shutdown()


@pytest.mark.asyncio
async def test_deleted_chat_continues_goal_cancellation_after_one_version_conflict(tmp_path, monkeypatch):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    goals = create_goal_service(work)
    first = goals.create(
        title="First", objective="First", owner_chat_id="chat-goal", goal_id="goal_first",
    )
    second = goals.create(
        title="Second", objective="Second", owner_chat_id="chat-goal", goal_id="goal_second",
    )
    original_cancel = goals.cancel_async

    async def conflict_first(goal_id, **kwargs):
        if goal_id == first.goal_id:
            raise GoalConflict("injected version churn")
        return await original_cancel(goal_id, **kwargs)

    monkeypatch.setattr(goals, "cancel_async", conflict_first)
    try:
        with pytest.raises(ExceptionGroup, match="could not be cancelled"):
            await goals.delete_chat("chat-goal")
        assert goals.repository.require_goal(second.goal_id).status == "cancelled"
        assert goals.repository.require_goal(first.goal_id).status == "draft"
    finally:
        await work.shutdown()


@pytest.mark.asyncio
async def test_cancelled_python_goal_effect_is_not_left_dispatched(tmp_path, monkeypatch):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    goals = create_goal_service(work)
    goal = goals.create(
        title="Kernel cancellation", objective="Stop accurately", owner_chat_id="chat-goal",
    )
    goal = goals.plan(
        goal.goal_id, [{"step_id": "python", "kind": "python", "config": {"code": "print(1)"}}],
        expected_version=goal.version,
    )
    step = goals.repository.get_step(goal.goal_id, "python")

    class Kernel:
        async def execute(self, **_kwargs):
            assert goals.repository.list_effects(goal.goal_id)[0].status == "dispatched"
            raise asyncio.CancelledError()

    kernel = Kernel()
    runtime = SimpleNamespace(kernel=kernel)
    host = SimpleNamespace(require_runtime=lambda: runtime)
    handlers = GoalHostHandlers(host, goals)
    monkeypatch.setattr(handlers, "_project_roots", lambda _scope: (str(tmp_path),))
    monkeypatch.setattr(
        "kernel_runtime.integration.preserve_persistent_kernel_workspace",
        lambda _kernel, _chat_id, roots, scope: (roots, scope),
    )
    context = StepExecutionContext(
        goal=goal, step=step, attempt=SimpleNamespace(attempt=1),
        scope=WorkScope(chat_id="chat-goal").to_dict(),
        cancellation_requested=lambda: True,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await handlers.python(context)
        assert goals.repository.list_effects(goal.goal_id)[0].status == "cancelled"
    finally:
        await work.shutdown()


@pytest.mark.asyncio
async def test_integration_effect_is_dispatched_before_review_side_effect(tmp_path):
    work = WorkService.open(str(tmp_path / "work.sqlite3"))
    goals = create_goal_service(work)
    config = {
        "review_id": "review-1", "integration_root": str(tmp_path),
        "target_branch": "main", "expected_target_oid": "abc",
        "expected_review_version": 1,
    }
    goal = goals.create(
        title="Integration", objective="Integrate once", owner_chat_id="chat-goal",
    )
    goal = goals.plan(
        goal.goal_id, [{"step_id": "integrate", "kind": "integration", "config": config}],
        expected_version=goal.version,
    )
    step = goals.repository.get_step(goal.goal_id, "integrate")
    observed = []

    class Review:
        review_id = "review-1"

        def to_dict(self):
            return {"review_id": self.review_id}

    def integrate(_review_id, **_kwargs):
        observed.append(goals.repository.list_effects(goal.goal_id)[0].status)
        return Review()

    artifacts = SimpleNamespace(put_json=lambda *_args, **_kwargs: SimpleNamespace(ref="artifact-1"))
    runtime = SimpleNamespace(
        coding=SimpleNamespace(review=SimpleNamespace(integrate_fast_forward=integrate)),
        session_artifacts=artifacts,
    )
    handlers = GoalHostHandlers(SimpleNamespace(require_runtime=lambda: runtime), goals)
    context = StepExecutionContext(
        goal=goal, step=step, attempt=SimpleNamespace(attempt=1),
        scope=WorkScope(chat_id="chat-goal").to_dict(),
        cancellation_requested=lambda: False,
    )
    try:
        result = await handlers.integration(context)
        assert result.status == "succeeded"
        assert observed == ["dispatched"]
        assert goals.repository.list_effects(goal.goal_id)[0].status == "succeeded"
    finally:
        await work.shutdown()
