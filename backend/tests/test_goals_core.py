from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from goals import create_goal_service
from goals.executor import StepExecutionResult
from goals.models import GoalConflict, GoalTransitionError, GoalValidationError
from goals.recovery import recover_goal_supervisors, reconcile_expired_step_leases
from work_fabric.service import WorkService


def _service(tmp_path, *, handlers=None, wait_resolvers=None):
    work = WorkService.open(str(tmp_path / "work.sqlite3"), worker_id="test-work")
    return work, create_goal_service(
        work, handlers=handlers, wait_resolvers=wait_resolvers,
    )


def _planned(service, steps, *, criteria=(), budget=None):
    goal = service.create(
        title="Durable goal", objective="Finish deterministically",
        success_criteria=criteria, budget=budget or {},
    )
    return service.plan(goal.goal_id, steps, expected_version=goal.version)


@pytest.mark.asyncio
async def test_live_step_renews_lease_and_concurrent_tick_does_not_expire_it(tmp_path, monkeypatch):
    entered, release, renewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    calls = []
    async def handler(context):
        calls.append(context.step.step_id)
        entered.set()
        await release.wait()
        assert not context.cancellation_requested()
        return StepExecutionResult()
    _work, service = _service(tmp_path, handlers={"python": handler})
    clock = [time.time()]
    monkeypatch.setattr("goals.repository.time.time", lambda: clock[0])
    service.supervisor.step_lease_ttl_s = 5
    original_heartbeat = service.repository.heartbeat_step
    def heartbeat(*args, **kwargs):
        result = original_heartbeat(*args, **kwargs)
        loop.call_soon_threadsafe(renewed.set)
        return result
    monkeypatch.setattr(service.repository, "heartbeat_step", heartbeat)
    goal = _planned(service, [{"step_id": "long", "kind": "python"}])
    service.start(goal.goal_id, expected_version=goal.version)
    first = asyncio.create_task(service.supervisor.tick(goal.goal_id))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        renewed.clear()
        clock[0] += 4
        await asyncio.wait_for(renewed.wait(), 3)
        clock[0] += 2
        reconcile_expired_step_leases(service, now=clock[0], goal_id=goal.goal_id)
        assert service.repository.get_step(goal.goal_id, "long").status == "running"
        second = asyncio.create_task(service.supervisor.tick(goal.goal_id))
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 3)
        assert calls == ["long"]
        assert service.repository.get_step(goal.goal_id, "long").status == "succeeded"
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done(): task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


def test_goal_mutations_share_atomic_work_event_outbox_and_versions(tmp_path):
    work, service = _service(tmp_path)
    goal = service.create(title="One", objective="Two", owner_chat_id="chat-1")
    assert goal.version == 1
    goal = service.plan(
        goal.goal_id,
        [
            {"step_id": "a", "kind": "wait", "wait_spec": {"source": "external"}},
            {"step_id": "b", "kind": "verification", "dependencies": ["a"]},
        ],
        expected_version=goal.version,
    )
    assert goal.version == 2
    with pytest.raises(GoalConflict):
        service.state_set(goal.goal_id, "x", 1, expected_version=1)
    goal = service.state_set(goal.goal_id, "x", {"ok": True}, expected_version=2)
    events = work.repository.list_events(
        aggregate_kind="goal", aggregate_id=goal.goal_id, limit=100
    )
    assert [event.aggregate_version for event in events] == [1, 2, 3]
    assert goal.version == len(events)
    with work.repository._read() as conn:
        outbox = conn.execute(
            "SELECT COUNT(*) FROM work_outbox o JOIN work_event e ON e.event_id=o.event_id "
            "WHERE e.aggregate_kind='goal' AND e.aggregate_id=?",
            (goal.goal_id,),
        ).fetchone()[0]
    assert outbox == len(events)


def test_explicit_goal_id_is_idempotent_only_for_identical_content(tmp_path):
    _work, service = _service(tmp_path)
    first = service.create(goal_id="goal-fixed", title="One", objective="Two")
    again = service.create(goal_id="goal-fixed", title="One", objective="Two")
    assert again == first
    with pytest.raises(GoalConflict, match="different content"):
        service.create(goal_id="goal-fixed", title="Changed", objective="Two")


def test_plan_rejects_cycles_and_cannot_be_silently_replaced(tmp_path):
    _work, service = _service(tmp_path)
    goal = service.create(title="Cycle", objective="Reject it")
    with pytest.raises(GoalValidationError, match="cycle"):
        service.plan(
            goal.goal_id,
            [
                {"step_id": "a", "kind": "wait", "dependencies": ["b"],
                 "wait_spec": {"source": "external"}},
                {"step_id": "b", "kind": "wait", "dependencies": ["a"],
                 "wait_spec": {"source": "external"}},
            ],
            expected_version=goal.version,
        )
    goal = service.plan(
        goal.goal_id, [{"step_id": "a", "kind": "wait",
                        "wait_spec": {"source": "external"}}],
        expected_version=goal.version,
    )
    with pytest.raises(GoalConflict, match="already has a plan"):
        service.plan(
            goal.goal_id, [{"step_id": "replacement", "kind": "wait",
                            "wait_spec": {"source": "external"}}],
            expected_version=goal.version,
        )


def test_time_wait_requires_explicit_clock_or_delay(tmp_path):
    _work, service = _service(tmp_path)
    goal = service.create(title="Clock", objective="Do not hang")

    with pytest.raises(GoalValidationError, match="wake_at or delay_s"):
        service.plan(
            goal.goal_id,
            [{"step_id": "clock", "kind": "wait"}],
            expected_version=goal.version,
        )


@pytest.mark.asyncio
async def test_time_wait_delay_is_resolved_when_step_starts(tmp_path):
    _work, service = _service(tmp_path)
    goal = _planned(service, [{
        "step_id": "clock",
        "kind": "wait",
        "wait_spec": {"source": "time", "delay_s": 30},
    }])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    before = time.time()

    await service.supervisor.tick(goal.goal_id)

    wait = service.repository.list_waits(goal.goal_id, status="pending")[0]
    assert wait.wake_at >= before + 29


@pytest.mark.asyncio
async def test_wait_releases_lease_and_wakes_without_model_or_kernel(tmp_path):
    _work, service = _service(tmp_path)
    goal = _planned(service, [
        {"step_id": "clock", "kind": "wait",
         "wait_spec": {"source": "time", "wake_at": time.time() - 1}},
        {"step_id": "verify", "kind": "verification", "dependencies": ["clock"]},
    ])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    first = await service.supervisor.tick(goal.goal_id)
    clock = service.repository.get_step(goal.goal_id, "clock")
    assert first["status"] == "waiting_external"
    assert first["resources_held_while_waiting"] is False
    assert clock.status == "waiting"
    assert clock.lease_owner == "" and clock.lease_expires_at == 0
    second = await service.supervisor.tick(goal.goal_id)
    assert second["status"] == "succeeded"
    assert all(step.status == "succeeded" for step in service.repository.list_steps(goal.goal_id))


@pytest.mark.asyncio
async def test_work_handler_persists_future_wake_without_holding_job_slot(tmp_path):
    work, service = _service(tmp_path)
    wake_at = time.time() + 120
    goal = _planned(service, [
        {"step_id": "clock", "kind": "wait",
         "wait_spec": {"source": "time", "wake_at": wake_at}},
    ])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    current = service.supervisor.enqueue(goal.goal_id, reason="current")
    execution = SimpleNamespace(job=current)
    result = await service.supervisor._work_handler(execution)
    replay = await service.supervisor._work_handler(execution)
    jobs = work.jobs.list(owner_kind="goal", owner_id=goal.goal_id)
    assert len(jobs) == 2
    successor = next(job for job in jobs if job.job_id != current.job_id)
    assert successor.available_at == pytest.approx(wake_at)
    assert successor.idempotency_key.endswith(f"poll-after:{current.job_id}")
    assert result.progress["waiting_job_holds_execution_slot"] is False
    assert replay.progress["waiting_job_holds_execution_slot"] is False


def test_startup_recovery_closes_mutation_to_enqueue_crash_window(tmp_path):
    work, service = _service(tmp_path)
    goal = _planned(service, [{
        "step_id": "clock", "kind": "wait",
        "wait_spec": {"source": "external"},
    }])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    assert work.jobs.list(owner_kind="goal", owner_id=goal.goal_id) == []
    first = recover_goal_supervisors(service)
    second = recover_goal_supervisors(service)
    assert first["scheduled"][0]["goal_id"] == goal.goal_id
    assert second["scheduled"][0]["job_id"] == first["scheduled"][0]["job_id"]
    assert len(work.jobs.list(owner_kind="goal", owner_id=goal.goal_id)) == 1


@pytest.mark.asyncio
async def test_startup_recovery_recreates_resolver_poll(tmp_path):
    work, service = _service(
        tmp_path, wait_resolvers={"external_status": lambda _wait: None},
    )
    goal = _planned(service, [{
        "step_id": "remote",
        "kind": "wait",
        "wait_spec": {"source": "external_status"},
    }])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    await service.supervisor.tick(goal.goal_id)
    assert service.get(goal.goal_id).status == "waiting_external"
    assert work.jobs.list(owner_kind="goal", owner_id=goal.goal_id) == []

    recovery_time = time.time()
    report = recover_goal_supervisors(service, now=recovery_time)

    assert len(report["scheduled"]) == 1
    assert report["scheduled"][0]["available_at"] == pytest.approx(recovery_time + 0.5)


def test_goal_cancel_is_one_atomic_parent_and_steps_mutation(tmp_path, monkeypatch):
    _work, service = _service(tmp_path)
    goal = _planned(service, [
        {"step_id": "one", "kind": "wait",
         "wait_spec": {"source": "external"}},
        {"step_id": "two", "kind": "wait",
         "wait_spec": {"source": "external"}},
    ])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    before = service.repository.require_goal(goal.goal_id)
    before_steps = service.repository.list_steps(goal.goal_id)
    original_advance = service.repository._advance_tx

    def fail_during_step_event(conn, current, *, event_type, **kwargs):
        if event_type == "goal.step_status":
            raise RuntimeError("injected cancellation write failure")
        return original_advance(conn, current, event_type=event_type, **kwargs)

    monkeypatch.setattr(service.repository, "_advance_tx", fail_during_step_event)
    with pytest.raises(RuntimeError, match="injected"):
        service.cancel(goal.goal_id, expected_version=goal.version)

    assert service.repository.require_goal(goal.goal_id) == before
    assert service.repository.list_steps(goal.goal_id) == before_steps

    monkeypatch.setattr(service.repository, "_advance_tx", original_advance)
    cancelled = service.cancel(goal.goal_id, expected_version=goal.version)
    assert cancelled.status == "cancelled"
    assert all(
        step.status == "cancelled"
        for step in service.repository.list_steps(goal.goal_id)
    )


def test_goal_recovery_keyset_scan_reaches_active_goal_after_row_500():
    goals = [
        SimpleNamespace(
            goal_id=f"goal-{index:04d}",
            status="running" if index == 500 else "draft",
        )
        for index in range(501)
    ]

    class Repository:
        def scan_goals(self, *, after_goal_id="", limit=500):
            return [row for row in goals if row.goal_id > after_goal_id][:limit]

        def require_goal(self, goal_id):
            return next(row for row in goals if row.goal_id == goal_id)

        def list_steps(self, _goal_id):
            return []

        def list_waits(self, _goal_id, status=""):
            assert status == "pending"
            return []

    scheduled = []

    class Supervisor:
        def enqueue(self, goal_id, *, reason, available_at):
            scheduled.append((goal_id, reason, available_at))
            return SimpleNamespace(job_id=f"job:{goal_id}", available_at=available_at)

    report = recover_goal_supervisors(SimpleNamespace(
        repository=Repository(), supervisor=Supervisor(),
    ), now=100.0)
    assert [item[0] for item in scheduled] == ["goal-0500"]
    assert report["scheduled"][0]["goal_id"] == "goal-0500"


@pytest.mark.asyncio
async def test_independent_injected_handlers_dispatch_and_unsupported_is_disclosed(tmp_path):
    entered = 0
    both_entered = asyncio.Event()

    async def handler(context):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        return {"status": "succeeded", "result_ref": f"result:{context.step.step_id}"}

    _work, service = _service(tmp_path, handlers={"python": handler})
    goal = _planned(service, [
        {"step_id": "one", "kind": "python"},
        {"step_id": "two", "kind": "python"},
    ])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    result = await service.supervisor.tick(goal.goal_id)
    assert result["status"] == "succeeded"
    assert entered == 2

    _work2, unsupported = _service(tmp_path / "unsupported")
    goal2 = _planned(unsupported, [{"step_id": "agent", "kind": "agent"}])
    goal2 = unsupported.start(goal2.goal_id, expected_version=goal2.version, enqueue=False)
    result2 = await unsupported.supervisor.tick(goal2.goal_id)
    step = unsupported.repository.get_step(goal2.goal_id, "agent")
    assert result2["status"] == "blocked"
    assert step.error == "unsupported_step_handler:agent"
    assert unsupported.snapshot(goal2.goal_id)["runtime_disclosure"]["missing_step_handlers"] == ["agent"]


@pytest.mark.asyncio
async def test_budget_exhaustion_prevents_step_attempt(tmp_path):
    called = False
    async def handler(_context):
        nonlocal called
        called = True
    _work, service = _service(tmp_path, handlers={"python": handler})
    goal = _planned(
        service, [{"step_id": "costly", "kind": "python"}],
        budget={"model_calls": 1},
    )
    goal = service.repository.update_budget_usage(
        goal.goal_id, {"model_calls": 1}, expected_version=goal.version
    )
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    result = await service.supervisor.tick(goal.goal_id)
    assert result["status"] == "paused"
    assert "exhausted" in result["paused_reason"]
    assert called is False
    assert service.repository.get_step(goal.goal_id, "costly").attempt_count == 0


@pytest.mark.asyncio
async def test_explicit_zero_budget_prevents_first_attempt(tmp_path):
    called = False

    async def handler(_context):
        nonlocal called
        called = True

    _work, service = _service(tmp_path, handlers={"python": handler})
    goal = _planned(
        service, [{"step_id": "costly", "kind": "python"}],
        budget={"model_calls": 0},
    )
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    result = await service.supervisor.tick(goal.goal_id)
    assert result["status"] == "paused"
    assert called is False


@pytest.mark.asyncio
async def test_deterministic_criterion_blocks_success_until_evidence(tmp_path):
    async def ok(_context):
        return {"status": "succeeded"}
    _work, service = _service(tmp_path, handlers={"process": ok})
    goal = _planned(
        service, [{"step_id": "run", "kind": "process"}],
        criteria=[{"id": "tests", "kind": "process_exit", "expected": 0}],
    )
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    first = await service.supervisor.tick(goal.goal_id)
    assert first["status"] == "blocked"
    report = service.verification_report(goal.goal_id)
    assert report.passed is False and report.checks[0].status == "failed"
    goal = service.repository.require_goal(goal.goal_id)
    goal = service.record_evidence(
        goal.goal_id, "tests", {"exit_code": 0, "recipe": "tests"},
        expected_version=goal.version,
    )
    goal = service.resume(goal.goal_id, expected_version=goal.version, enqueue=False)
    second = await service.supervisor.tick(goal.goal_id)
    assert second["status"] == "succeeded"


@pytest.mark.asyncio
async def test_mid_plan_verification_does_not_require_later_steps(tmp_path):
    async def ok(_context):
        return {"status": "succeeded"}

    _work, service = _service(tmp_path, handlers={"python": ok})
    goal = _planned(service, [
        {"step_id": "prepare", "kind": "python"},
        {"step_id": "gate", "kind": "verification", "dependencies": ["prepare"],
         "verification_spec": {"criteria": [
             {"id": "gate-proof", "kind": "process_exit", "expected": 0},
         ]}},
        {"step_id": "later", "kind": "wait", "dependencies": ["gate"],
         "wait_spec": {"source": "time", "wake_at": time.time() - 1}},
    ])
    goal = service.record_evidence(
        goal.goal_id, "gate-proof", {"exit_code": 0},
        expected_version=goal.version,
    )
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    await service.supervisor.tick(goal.goal_id)
    await service.supervisor.tick(goal.goal_id)
    assert service.repository.get_step(goal.goal_id, "gate").status == "succeeded"
    assert service.repository.get_step(goal.goal_id, "later").status == "pending"


@pytest.mark.asyncio
async def test_input_attention_is_versioned_and_releases_resources(tmp_path):
    _work, service = _service(tmp_path)
    goal = _planned(service, [
        {"step_id": "choice", "kind": "input",
         "config": {"prompt": "Choose", "schema": {"type": "string"}}},
    ])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    result = await service.supervisor.tick(goal.goal_id)
    assert result["status"] == "waiting_user"
    attention = service.repository.list_attention(goal.goal_id, status="open")[0]
    interaction = service.work.interactions.get(attention.attention_id)
    assert interaction.kind == "goal_input"
    assert interaction.owner_id == goal.goal_id
    step = service.repository.get_step(goal.goal_id, "choice")
    assert step.lease_owner == ""
    assert service.repository.list_attempts(goal.goal_id)[0].status == "waiting"
    current = service.repository.require_goal(goal.goal_id)
    current = service.answer_input(
        attention.attention_id, "main", expected_version=current.version,
        expected_attention_version=attention.version, enqueue=False,
    )
    assert service.state_get(goal.goal_id, "input:choice") == "main"
    assert service.work.interactions.get(attention.attention_id).response == "main"
    assert service.repository.list_attempts(goal.goal_id)[0].status == "succeeded"
    final = await service.supervisor.tick(goal.goal_id)
    assert final["status"] == "succeeded"
    with pytest.raises(GoalConflict):
        service.answer_input(
            attention.attention_id, "again", expected_version=service.get(goal.goal_id).version,
            expected_attention_version=attention.version, enqueue=False,
        )


@pytest.mark.asyncio
async def test_skipped_inline_goal_input_closes_wait_and_blocks_goal(tmp_path):
    _work, service = _service(tmp_path)
    goal = _planned(service, [{
        "step_id": "choice",
        "kind": "input",
        "config": {"prompt": "Choose", "schema": {"type": "string"}},
    }])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    result = await service.supervisor.tick(goal.goal_id)
    assert result["status"] == "waiting_user"
    attention = service.repository.list_attention(goal.goal_id, status="open")[0]

    service.dismiss_input(
        attention.attention_id,
        expected_attention_version=attention.version,
        enqueue=False,
    )

    assert service.work.interactions.get(attention.attention_id).status == "dismissed"
    assert service.repository.list_attention(goal.goal_id)[0].status == "dismissed"
    assert service.repository.list_waits(goal.goal_id)[0].status == "cancelled"
    assert service.repository.get_step(goal.goal_id, "choice").status == "blocked"
    final = await service.supervisor.tick(goal.goal_id)
    assert final["status"] == "blocked"


@pytest.mark.asyncio
async def test_retry_request_at_attempt_limit_becomes_blocked_not_job_error(tmp_path):
    async def retry(_context):
        return {"status": "retry_scheduled", "error": "try again"}

    _work, service = _service(tmp_path, handlers={"python": retry})
    goal = _planned(service, [
        {"step_id": "once", "kind": "python", "max_attempts": 1},
    ])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    result = await service.supervisor.tick(goal.goal_id)
    assert result["status"] == "blocked"
    assert service.repository.get_step(goal.goal_id, "once").status == "failed"


def test_attempt_projection_requires_exact_matching_cursor(tmp_path):
    _work, service = _service(tmp_path)
    goal = _planned(service, [{"step_id": "agent", "kind": "agent"}])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    goal = service.repository.transition_goal(
        goal.goal_id, "running", expected_version=goal.version
    )
    goal, _ = service.repository.set_step_status(
        goal.goal_id, "agent", "ready", expected_version=goal.version
    )
    goal, step, attempt = service.repository.lease_step(
        goal.goal_id, "agent", expected_version=goal.version, lease_owner="test"
    )
    with pytest.raises(GoalConflict, match="thread"):
        service.repository.update_attempt_projection(
            goal.goal_id, step.step_id, expected_version=goal.version,
            attempt=attempt.attempt,
            snapshot_cursor={"thread_id": "wrong", "sequence": 1, "snapshot_id": "snap"},
            machine_revision="machine", native_run_id="run",
        )
    stored = service.repository.update_attempt_projection(
        goal.goal_id, step.step_id, expected_version=goal.version,
        attempt=attempt.attempt,
        snapshot_cursor={"thread_id": attempt.snapshot_thread_id, "sequence": 1,
                         "snapshot_id": "snap"},
        machine_revision="machine", native_run_id="run",
    )
    assert stored.snapshot_cursor["snapshot_id"] == "snap"


def test_effect_idempotency_survives_stale_caller_version(tmp_path):
    _work, service = _service(tmp_path)
    goal = _planned(service, [{"step_id": "process", "kind": "process"}])
    effect = service.repository.record_effect(
        goal.goal_id, "process", expected_version=goal.version,
        kind="process.spawn", idempotency_key="same", request={"recipe": "x"},
    )
    replay = service.repository.record_effect(
        goal.goal_id, "process", expected_version=goal.version,
        kind="process.spawn", idempotency_key="same", request={"recipe": "x"},
    )
    assert replay.effect_id == effect.effect_id
    current = service.get(goal.goal_id)
    dispatched = service.repository.update_effect(
        effect.effect_id, expected_version=current.version, status="dispatched"
    )
    replayed = service.repository.update_effect(
        effect.effect_id, expected_version=current.version, status="dispatched"
    )
    assert replayed == dispatched


@pytest.mark.asyncio
async def test_stale_attempt_result_cannot_complete_newer_retry(tmp_path):
    _work, service = _service(tmp_path)
    goal = _planned(service, [
        {"step_id": "retry", "kind": "python", "max_attempts": 2},
    ])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    goal = service.repository.transition_goal(
        goal.goal_id, "running", expected_version=goal.version
    )
    goal, _ = service.repository.set_step_status(
        goal.goal_id, "retry", "ready", expected_version=goal.version
    )
    goal, leased_one, attempt_one = service.repository.lease_step(
        goal.goal_id, "retry", expected_version=goal.version,
        lease_owner="attempt-one",
    )
    goal, running_one, attempt_one = service.repository.start_step(
        goal.goal_id, "retry", expected_version=goal.version,
        lease_owner=leased_one.lease_owner, lease_epoch=leased_one.lease_epoch,
    )
    goal, _ = service.repository.finish_step(
        goal.goal_id, "retry", expected_version=goal.version,
        status="blocked", error="lease expired",
    )
    goal, _ = service.repository.retry_step(
        goal.goal_id, "retry", expected_version=goal.version
    )
    goal, _ = service.repository.set_step_status(
        goal.goal_id, "retry", "ready", expected_version=goal.version
    )
    goal, leased_two, attempt_two = service.repository.lease_step(
        goal.goal_id, "retry", expected_version=goal.version,
        lease_owner="attempt-two",
    )
    goal, running_two, attempt_two = service.repository.start_step(
        goal.goal_id, "retry", expected_version=goal.version,
        lease_owner=leased_two.lease_owner, lease_epoch=leased_two.lease_epoch,
    )

    await service.supervisor._apply_result(
        goal, running_one, attempt_one,
        StepExecutionResult(status="succeeded", result_ref="stale-result"),
    )

    current = service.repository.get_step(goal.goal_id, "retry")
    assert current.status == "running"
    assert current.attempt_count == attempt_two.attempt == 2
    assert current.lease_epoch == running_two.lease_epoch
    assert current.result_ref == ""


def test_recovery_fences_uncertain_effect_instead_of_replaying(tmp_path):
    _work, service = _service(tmp_path)
    goal = _planned(service, [{"step_id": "process", "kind": "process"}])
    goal = service.start(goal.goal_id, expected_version=goal.version, enqueue=False)
    goal = service.repository.transition_goal(goal.goal_id, "running", expected_version=goal.version)
    goal, _ = service.repository.set_step_status(
        goal.goal_id, "process", "ready", expected_version=goal.version
    )
    goal, step, _attempt = service.repository.lease_step(
        goal.goal_id, "process", expected_version=goal.version,
        lease_owner="dead", lease_ttl_s=5,
    )
    goal, step, _attempt = service.repository.start_step(
        goal.goal_id, "process", expected_version=goal.version,
        lease_owner="dead", lease_epoch=step.lease_epoch,
    )
    effect = service.repository.record_effect(
        goal.goal_id, step.step_id, expected_version=goal.version,
        kind="process.spawn", idempotency_key="spawn-1", status="planned",
    )
    goal = service.repository.require_goal(goal.goal_id)
    service.repository.update_effect(
        effect.effect_id, expected_version=goal.version, status="dispatched"
    )
    with service.repository.work._write() as conn:
        conn.execute("UPDATE workflow_step SET lease_expires_at=? WHERE step_id=?",
                     (time.time() - 1, step.step_id))
    report = reconcile_expired_step_leases(service)
    assert report["recovered"][0]["status"] == "unknown_effect"
    assert service.repository.get_step(goal.goal_id, step.step_id).status == "blocked"
    assert service.repository.list_effects(goal.goal_id)[0].status == "unknown_effect"


@pytest.mark.asyncio
async def test_sync_step_cancellation_revokes_predicate_and_waits_for_worker():
    import threading
    from types import SimpleNamespace
    from goals.executor import StepExecutor, StepExecutionContext
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    effects = []
    def handler(context):
        entered.set()
        while not context.cancellation_requested():
            release.wait(.005)
        cancelled.set()
        release.wait(2)
        effects.append("settled")
    executor = StepExecutor({"python": handler})
    context = StepExecutionContext(goal=None, step=SimpleNamespace(kind="python"), attempt=None, scope={})
    task = asyncio.create_task(executor.execute(context))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        assert await asyncio.to_thread(cancelled.wait, 1)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert effects == ["settled"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
