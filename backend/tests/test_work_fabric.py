"""Phase 0 Work Fabric durability, scheduling, and recovery contracts."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from core_invariants import InjectedFault, inject_faults
from work_fabric.jobs import JobResult
from work_fabric.models import WorkConflict
from work_fabric.repository import WorkRepository
from work_fabric.scope import WorkScope
from work_fabric.service import WorkService
from capability_broker import CapabilityBroker, InvocationContext
from tools import ToolRegistry
from work_fabric.capabilities import register_work_fabric_tools
from work_fabric.handles import job_handle_envelope
from tests.support.astb_runtime import StaticRuntimeRegistry


def _service(tmp_path) -> WorkService:
    return WorkService.open(str(tmp_path / "work.sqlite3"), worker_id="test-worker")


def test_event_and_outbox_commit_together_and_replay_idempotently(tmp_path):
    service = _service(tmp_path)
    scope = WorkScope(chat_id="chat-1", workspace_id="workspace-1")

    event = service.events.publish(
        "goal.created",
        aggregate_kind="goal",
        aggregate_id="goal-1",
        scope=scope,
        idempotency_key="create-goal-1",
        payload={"title": "Ship it"},
    )
    replay = service.events.publish(
        "goal.created",
        aggregate_kind="goal",
        aggregate_id="goal-1",
        scope=scope,
        idempotency_key="create-goal-1",
        payload={"title": "Ship it"},
    )

    assert replay == event
    with sqlite3.connect(service.repository.path) as conn:
        event_count = conn.execute("SELECT COUNT(*) FROM work_event").fetchone()[0]
        outbox = conn.execute(
            "SELECT event_id, event_sequence, status FROM work_outbox"
        ).fetchone()
    assert event_count == 1
    assert outbox == (event.event_id, event.sequence, "pending")
    with pytest.raises(WorkConflict, match="reused"):
        service.events.publish(
            "goal.deleted",
            aggregate_kind="goal",
            aggregate_id="goal-1",
            scope=scope,
            idempotency_key="create-goal-1",
        )


def test_shared_precommit_fault_rolls_back_event_and_outbox_together(tmp_path):
    service = _service(tmp_path)
    with inject_faults("work.before_commit"):
        with pytest.raises(InjectedFault, match="work.before_commit"):
            service.events.publish(
                "goal.created",
                aggregate_kind="goal",
                aggregate_id="goal-fault",
                scope=WorkScope(chat_id="chat-fault"),
                payload={"title": "must roll back"},
            )

    with sqlite3.connect(service.repository.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM work_event").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM work_outbox").fetchone()[0] == 0


def test_operation_receipt_fingerprint_ignores_retry_origin_but_binds_payload(tmp_path):
    service = _service(tmp_path)
    scope = WorkScope(chat_id="chat-operation", workspace_revision=3)
    request = {
        "capability": {"capability_id": "write_value", "schema_revision": "v1"},
        "arguments_sha256": "a" * 64,
        "attribution": {"cell_origin": {"cell_execution_id": "cell-1"}},
    }
    first = service.repository.record_operation_receipt(
        operation_id="receipt-1",
        kind="write_value",
        status="succeeded",
        scope=scope,
        request=request,
        response={"ok": True},
        idempotency_key="stable-effect",
    )
    replay = service.repository.record_operation_receipt(
        operation_id="receipt-2",
        kind="write_value",
        status="succeeded",
        scope=scope,
        request={
            **request,
            "attribution": {"cell_origin": {"cell_execution_id": "cell-2"}},
        },
        response={"ok": True},
        idempotency_key="stable-effect",
    )
    assert replay.operation_id == first.operation_id
    assert len(first.request_fingerprint) == 64

    with pytest.raises(WorkConflict, match="different request"):
        service.repository.record_operation_receipt(
            operation_id="receipt-3",
            kind="write_value",
            status="succeeded",
            scope=scope,
            request={**request, "arguments_sha256": "b" * 64},
            response={"ok": True},
            idempotency_key="stable-effect",
        )


def test_work_events_and_jobs_apply_full_scope_before_limit(tmp_path):
    service = _service(tmp_path)
    wanted_scope = WorkScope(
        chat_id="chat-wanted",
        workspace_id="workspace-wanted",
        workspace_revision=4,
        catalog_release_id="catalog-wanted",
    )
    wanted_event = service.events.publish(
        "wanted",
        aggregate_kind="test",
        aggregate_id="wanted",
        scope=wanted_scope,
    )
    wanted_job = service.jobs.create(
        "wanted.job",
        owner_kind="chat",
        owner_id="chat-wanted",
        scope=wanted_scope,
    )
    for index in range(3):
        foreign = WorkScope(
            chat_id="chat-wanted",
            workspace_id="workspace-wanted",
            workspace_revision=4,
            catalog_release_id=f"catalog-foreign-{index}",
        )
        service.events.publish(
            "foreign",
            aggregate_kind="test",
            aggregate_id=f"foreign-{index}",
            scope=foreign,
        )
        service.jobs.create(
            "foreign.job",
            owner_kind="chat",
            owner_id="chat-wanted",
            scope=foreign,
        )

    events = service.events.list(scope=wanted_scope, limit=1)
    jobs = service.jobs.list(scope=wanted_scope, limit=1)
    assert [event.event_id for event in events] == [wanted_event.event_id]
    assert [job.job_id for job in jobs] == [wanted_job.job_id]


def test_event_idempotency_rejects_changed_version_scope_and_payload(tmp_path):
    service = _service(tmp_path)
    original = service.events.publish(
        "artifact.revised",
        aggregate_kind="artifact",
        aggregate_id="artifact-1",
        aggregate_version=1,
        expected_aggregate_version=0,
        scope=WorkScope(chat_id="chat-a"),
        idempotency_key="revision-key",
        payload={"revision": 1},
    )
    replay = service.events.publish(
        "artifact.revised",
        aggregate_kind="artifact",
        aggregate_id="artifact-1",
        aggregate_version=1,
        expected_aggregate_version=0,
        scope=WorkScope(chat_id="chat-a"),
        idempotency_key="revision-key",
        payload={"revision": 1},
    )
    assert replay == original

    changed = (
        {"aggregate_version": 2, "expected_aggregate_version": 1},
        {"scope": WorkScope(chat_id="chat-b")},
        {"payload": {"revision": 2}},
    )
    for override in changed:
        kwargs = {
            "aggregate_version": 1,
            "expected_aggregate_version": 0,
            "scope": WorkScope(chat_id="chat-a"),
            "payload": {"revision": 1},
            **override,
        }
        with pytest.raises(WorkConflict, match="reused"):
            service.events.publish(
                "artifact.revised",
                aggregate_kind="artifact",
                aggregate_id="artifact-1",
                idempotency_key="revision-key",
                **kwargs,
            )


@pytest.mark.asyncio
async def test_one_durable_interaction_supports_live_wait_and_later_lookup(tmp_path):
    service = _service(tmp_path)
    scope = WorkScope(chat_id="chat-interaction", workspace_id="workspace-1")
    record = service.interactions.create(
        kind="clarification",
        prompt="Choose a direction",
        schema={"questions": [{"id": "q1"}]},
        metadata={"title": "Direction", "source": "ask_user"},
        owner_kind="run",
        owner_id="run-1",
        scope=scope,
        idempotency_key="ask-1",
    )
    waiter = asyncio.create_task(
        service.interactions.wait(record.interaction_id, timeout_s=5)
    )
    await asyncio.sleep(0)
    answered = service.interactions.resolve(
        record.interaction_id,
        status="answered",
        response={"answers": {"q1": "Build"}, "skipped": False},
        expected_version=record.version,
    )
    observed = await waiter

    assert observed == answered
    assert observed.version == 2
    assert service.interactions.get(record.interaction_id) == observed
    assert [event.event_type for event in service.events.list(
        aggregate_kind="interaction",
        aggregate_id=record.interaction_id,
    )] == ["interaction.requested", "interaction.answered"]


def test_interaction_scope_is_applied_before_order_and_limit(tmp_path):
    service = _service(tmp_path)
    wanted_scope = WorkScope(chat_id="chat-wanted", workspace_id="workspace-wanted")
    wanted = service.interactions.create(
        kind="question", prompt="Wanted", owner_kind="chat",
        owner_id="chat-wanted", scope=wanted_scope,
    )
    for index in range(3):
        service.interactions.create(
            kind="question", prompt=f"Other {index}", owner_kind="chat",
            owner_id="chat-other", scope=WorkScope(chat_id="chat-other"),
        )

    visible = service.interactions.list(scope=wanted_scope, limit=1)

    assert [item.interaction_id for item in visible] == [wanted.interaction_id]


def test_job_idempotency_rejects_every_changed_creation_argument(tmp_path):
    service = _service(tmp_path)
    baseline = {
        "owner_kind": "goal",
        "owner_id": "goal-1",
        "scope": WorkScope(chat_id="chat-a", workspace_id="workspace-a"),
        "priority": 3,
        "input_manifest": {"value": 1},
        "artifact_refs": ("artifact://sha256/" + "a" * 64,),
        "retry_policy": {"base_delay_s": 1},
        "max_attempts": 2,
        "idempotency_key": "job-key",
        "available_at": 100.0,
        "job_id": "job-explicit",
    }
    first = service.jobs.create("test.job", **baseline)
    assert len(first.request_fingerprint) == 64
    assert service.jobs.create("test.job", **baseline) == first
    changes = (
        {"scope": WorkScope(chat_id="chat-b", workspace_id="workspace-a")},
        {"priority": 4},
        {"input_manifest": {"value": 2}},
        {"artifact_refs": ("artifact://sha256/" + "b" * 64,)},
        {"retry_policy": {"base_delay_s": 2}},
        {"max_attempts": 3},
        {"available_at": 101.0},
        {"job_id": "job-other"},
    )
    for override in changes:
        with pytest.raises(WorkConflict, match="different arguments"):
            service.jobs.create("test.job", **{**baseline, **override})


@pytest.mark.asyncio
async def test_event_outbox_delivers_and_advances_restart_safe_status(tmp_path):
    service = _service(tmp_path)
    delivered = []
    service.events.subscribe(lambda event: delivered.append(event.event_id))
    event = service.events.publish(
        "work.changed",
        aggregate_kind="work",
        aggregate_id="work-1",
    )

    assert await service.events.dispatch_once() == 1
    assert delivered == [event.event_id]
    with sqlite3.connect(service.repository.path) as conn:
        status = conn.execute(
            "SELECT status FROM work_outbox WHERE event_id=?", (event.event_id,)
        ).fetchone()[0]
    assert status == "delivered"


@pytest.mark.asyncio
async def test_scheduler_completes_a_durable_job_and_survives_service_lifecycle(tmp_path):
    service = _service(tmp_path)
    observed = []

    async def handler(context):
        observed.append(context.job.job_id)
        context.progress({"phase": "running", "percent": 50})
        return JobResult(
            result_ref="artifact://sha256/result",
            progress={"phase": "complete", "percent": 100},
        )

    service.register_job_handler("test.echo", handler)
    created = service.jobs.create(
        "test.echo",
        owner_kind="chat",
        owner_id="chat-1",
        scope=WorkScope(chat_id="chat-1"),
        input_manifest={"value": 7},
        idempotency_key="echo-7",
    )
    duplicate = service.jobs.create(
        "test.echo",
        owner_kind="chat",
        owner_id="chat-1",
        scope=WorkScope(chat_id="chat-1"),
        input_manifest={"value": 7},
        idempotency_key="echo-7",
    )
    assert duplicate.job_id == created.job_id

    await service.start()
    completed = await service.jobs.wait(created.job_id, timeout_s=5)
    await service.shutdown()
    await service.shutdown()
    assert observed == [created.job_id]
    assert completed.status == "succeeded"
    assert completed.result_ref == "artifact://sha256/result"
    assert completed.progress == {"phase": "complete", "percent": 100}
    assert service.started is False
    types = [
        event.event_type
        for event in service.events.list(
            aggregate_kind="job", aggregate_id=created.job_id, limit=50
        )
    ]
    assert types == [
        "job.created",
        "job.leased",
        "job.started",
        "job.progressed",
        "job.succeeded",
    ]


@pytest.mark.asyncio
async def test_per_kind_capacity_leaves_queued_jobs_unleased_and_other_work_runnable(
    tmp_path,
):
    service = _service(tmp_path)
    limited_started = []
    limited_entered = asyncio.Event()
    unrelated_entered = asyncio.Event()
    release = asyncio.Event()

    async def limited(context):
        limited_started.append(context.job.job_id)
        limited_entered.set()
        await release.wait()
        return JobResult()

    async def unrelated(_context):
        unrelated_entered.set()
        return JobResult()

    service.register_job_handler(
        "test.limited",
        limited,
        max_concurrency=1,
    )
    service.register_job_handler("test.unrelated", unrelated)
    first = service.jobs.create("test.limited")
    second = service.jobs.create("test.limited")
    other = service.jobs.create("test.unrelated")

    await service.start()
    try:
        await asyncio.wait_for(limited_entered.wait(), timeout=2)
        await asyncio.wait_for(unrelated_entered.wait(), timeout=2)
        assert limited_started == [first.job_id]
        assert service.jobs.require(second.job_id).status == "queued"
        assert (await service.jobs.wait(other.job_id, timeout_s=2)).status == "succeeded"
        release.set()
        assert (await service.jobs.wait(first.job_id, timeout_s=2)).status == "succeeded"
        assert (await service.jobs.wait(second.job_id, timeout_s=2)).status == "succeeded"
    finally:
        release.set()
        await service.shutdown()


@pytest.mark.asyncio
async def test_scheduler_cancellation_reaches_sync_handler_before_shutdown_returns(
    tmp_path,
):
    service = _service(tmp_path)
    entered = threading.Event()
    settled = threading.Event()

    def handler(context):
        entered.set()
        while not context.cancellation_requested():
            time.sleep(0.01)
        settled.set()
        return JobResult()

    service.register_job_handler("test.cancellable-sync", handler)
    service.jobs.create("test.cancellable-sync")
    await service.start()
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()

    await asyncio.wait_for(service.shutdown(), timeout=5)

    assert settled.is_set()
    assert service.started is False


def test_job_identity_reacquires_after_service_reopen(tmp_path):
    path = str(tmp_path / "work.sqlite3")
    first = WorkService.open(path, worker_id="first-worker")
    created = first.jobs.create(
        "test.persist",
        owner_kind="chat",
        owner_id="chat-1",
        scope=WorkScope(chat_id="chat-1"),
    )

    reopened = WorkService.open(path, worker_id="second-worker")
    recovered = reopened.jobs.require(created.job_id)

    assert recovered == created
    assert recovered.job_id == created.job_id
    assert recovered.revision == created.revision


def test_recovery_distinguishes_undispatched_and_unknown_running_effects(tmp_path):
    service = _service(tmp_path)
    queued = service.jobs.create(
        "test.recover",
        owner_kind="system",
        owner_id="variant1",
        max_attempts=2,
        available_at=1,
    )
    leased = service.repository.lease_next_job(
        "worker-a", kinds=("test.recover",), lease_ttl_s=2, now=10
    )
    assert leased is not None and leased.job_id == queued.job_id

    running_created = service.jobs.create(
        "test.running",
        owner_kind="system",
        owner_id="variant1",
        max_attempts=2,
        available_at=1,
    )
    running_lease = service.repository.lease_next_job(
        "worker-b", kinds=("test.running",), lease_ttl_s=2, now=10
    )
    assert running_lease is not None
    service.repository.start_job(
        running_lease.job_id,
        lease_owner=running_lease.lease_owner,
        lease_epoch=running_lease.lease_epoch,
        now=10,
    )

    report = service.recovery.run_once(now=20)

    assert service.jobs.require(leased.job_id).status == "queued"
    assert service.jobs.require(running_lease.job_id).status == "unknown_effect"
    assert report.recovered_jobs == 2
    assert report.unknown_effect_jobs == 1


def test_heartbeat_renews_lease_without_invalidating_control_revision(tmp_path):
    service = _service(tmp_path)
    created = service.jobs.create(
        "test.heartbeat", owner_kind="chat", owner_id="chat-heartbeat",
        scope=WorkScope(chat_id="chat-heartbeat"), available_at=1,
    )
    leased = service.repository.lease_next_job(
        "worker-heartbeat", kinds=("test.heartbeat",), lease_ttl_s=30, now=10,
    )
    assert leased is not None and leased.job_id == created.job_id
    running = service.repository.start_job(
        leased.job_id,
        lease_owner=leased.lease_owner,
        lease_epoch=leased.lease_epoch,
        now=10,
    )

    heartbeat = service.repository.heartbeat_job(
        running.job_id,
        lease_owner=running.lease_owner,
        lease_epoch=running.lease_epoch,
        lease_ttl_s=30,
        now=11,
    )

    assert heartbeat.revision == running.revision
    cancelled = service.jobs.cancel(
        running.job_id, expected_revision=running.revision,
    )
    assert cancelled.cancel_requested is True


@pytest.mark.asyncio
async def test_periodic_recovery_reclaims_a_lease_that_expires_after_startup(
    tmp_path, monkeypatch,
):
    service = WorkService.open(
        str(tmp_path / "work.sqlite3"),
        worker_id="recovery-worker",
        recovery_interval_s=0.02,
    )
    created = service.jobs.create(
        "test.future-expiry",
        owner_kind="system",
        owner_id="variant1",
        max_attempts=2,
        available_at=1,
    )
    leased = service.repository.lease_next_job(
        "dead-worker",
        kinds=("test.future-expiry",),
        lease_ttl_s=2,
        now=10,
    )
    assert leased is not None
    scans = 0

    async def controlled_recovery(*, now=None):
        nonlocal scans
        del now
        scans += 1
        # Startup occurs before expiry; the periodic scan occurs afterward.
        return service.recovery.run_once(now=11 if scans == 1 else 20)

    monkeypatch.setattr(service.recovery, "run_once_async", controlled_recovery)
    await service.start()
    try:
        for _ in range(100):
            if service.jobs.require(created.job_id).status == "queued":
                break
            await asyncio.sleep(0.01)
        recovered = service.jobs.require(created.job_id)
        assert scans >= 2
        assert recovered.status == "queued"
        assert recovered.lease_owner == ""
    finally:
        await service.shutdown()


def test_receipts_become_scoped_idempotent_operations(tmp_path):
    service = _service(tmp_path)
    scope = WorkScope(chat_id="chat-1", goal_id="goal-1", step_id="step-1")
    receipt = {
        "receipt_id": "receipt-1",
        "status": "needs_reconciliation",
        "capability": {"capability_id": "desktop.click"},
        "arguments_sha256": "abc",
        "error": {"message": "confirmation lost", "may_have_applied": True},
        "effect": {"idempotency_key": "effect-1"},
        "attribution": {
            "principal_actor_id": "model",
            "nested_call_id": "nested-1",
            "work_scope": scope.to_dict(),
        },
    }

    first = service.ingest_capability_receipt(receipt)
    replay = service.ingest_capability_receipt(receipt)
    effect_replay = service.ingest_capability_receipt({
        **receipt,
        "receipt_id": "receipt-2",
    })
    assert replay == first
    assert effect_replay == first
    assert first.status == "unknown_effect"
    assert first.scope == scope


@pytest.mark.asyncio
async def test_returned_job_handles_wait_cancel_and_reject_stale_revisions(tmp_path):
    service = _service(tmp_path)
    registry = ToolRegistry()
    runtime = SimpleNamespace(work=service, registry=registry)
    host = SimpleNamespace(
        registry=registry,
        require_runtime=lambda: runtime,
        capability_broker=None,
    )
    broker = CapabilityBroker(
        registry=registry,
        runtime_registry=StaticRuntimeRegistry(),
        enabled_resolver=lambda: {tool.name for tool in registry.all()},
    )
    runtime.broker = broker
    register_work_fabric_tools(host)
    created = service.jobs.create(
        "test.manual",
        owner_kind="chat",
        owner_id="chat-1",
        scope=WorkScope(chat_id="chat-1"),
    )
    context = InvocationContext(
        chat_id="chat-1",
        run_id="run-1",
        outer_tool_call_id="outer-1",
        cell_execution_id="cell-1",
        nested_call_id="get-1",
        catalog_release_id="catalog-1",
        work_scope=WorkScope(chat_id="chat-1"),
        surface="ipython",
    )

    envelope = job_handle_envelope(
        created, broker=broker, context=context,
    )
    waited_with = {}

    async def fake_wait(job_id, *, timeout_s):
        waited_with.update(job_id=job_id, timeout_s=timeout_s)
        return service.jobs.require(job_id)

    original_wait = service.jobs.wait
    service.jobs.wait = fake_wait
    identity = envelope["$variant1_handle"]
    try:
        waited = await broker.invoke_name(
            "remote_handle_dispatch",
            {
                "handle": {
                    key: identity[key]
                    for key in ("service", "kind", "id", "generation", "revision")
                },
                "method": "wait",
                "arguments": {},
            },
            InvocationContext(**{
                **context.__dict__,
                "nested_call_id": "wait-1",
            }),
        )
    finally:
        service.jobs.wait = original_wait
    dispatch_context = InvocationContext(
        **{
            **context.__dict__,
            "nested_call_id": "dispatch-1",
        }
    )
    cancelled = await broker.invoke_name(
        "remote_handle_dispatch",
        {
            "handle": {
                key: identity[key]
                for key in ("service", "kind", "id", "generation", "revision")
            },
            "method": "cancel",
            "arguments": {"reason": "done"},
        },
        dispatch_context,
    )
    stale_context = InvocationContext(
        **{
            **context.__dict__,
            "nested_call_id": "dispatch-2",
        }
    )
    stale = await broker.invoke_name(
        "remote_handle_dispatch",
        {
            "handle": {
                key: identity[key]
                for key in ("service", "kind", "id", "generation", "revision")
            },
            "method": "state",
            "arguments": {},
        },
        stale_context,
    )

    assert waited.ok
    assert waited_with == {"job_id": created.job_id, "timeout_s": 30.0}
    assert identity["revision"] == created.revision
    assert cancelled.ok
    assert cancelled.result_value["$variant1_handle"]["revision"] > created.revision
    assert not stale.ok
    assert stale.error is not None
    assert "stale work.job handle" in stale.error.message
